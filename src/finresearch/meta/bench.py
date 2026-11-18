"""The meta-research bench: the whole pipeline, run on worlds with nothing to find (step 3.12).

Decision 0024. One simulated hypothesis is one complete piece of research,
following the protocol's life cycle on a synthetic world:

1. **A world**: ``meta.noise`` returns for ``instruments x days``, cut into
   exploration (first 60%), confirmation (next 25%, its first ``embargo_days``
   skipped) and a vault that the bench never touches.
2. **Exploration**: every variant of a family is run on the exploration
   segment; the one with the best Sharpe ratio is kept.
3. **Registration**: that variant and the default thresholds are frozen.
4. **Confirmation**: the kept variant runs once on the confirmation segment;
   its statistics go to ``verdict.judge``. On noise, "supported" is a false
   positive.

**Strategies.** A variant ranks the instruments every day by a signal (the
sum of the last ``lookback`` values of a base series: the returns themselves
for the momentum family, a feature for the many-hypotheses mode) and holds
weights proportional to the demeaned rank, scaled to gross exposure 1 and
net 0; ``sign`` -1 trades against the signal (reversal). A ``holding``-day
variant holds the average of the last ``holding`` days' weights (overlapping
cohorts). Day ``t``'s P&L is the position held from the end of ``t - 1``
times the returns of ``t``, less ``cost`` per unit of gross exposure traded.

**The seven statistics** (decision 0024, choice 2):

- **t**, two ways, both judged: Newey-West with lag ``holding - 1`` on the
  daily P&L, and the ordinary t of non-overlapping returns (one cohort formed
  every ``holding`` days and held ``holding`` days, ``stats.hac``);
- **deflated Sharpe ratio** of the confirmation P&L: its probabilistic Sharpe
  ratio against the expected best Sharpe ratio of the family's exploration
  trials under no edge, simulated from their correlation matrix (decision
  0025, ``stats.maxima``); ONC still reports how the trials group;
- **PBO** by CSCV on the family's exploration P&L (3.7);
- **costs doubled** and **without the best two days**: the mean confirmation
  P&L so changed;
- **perturbation**: the grid's neighbours (one step in lookback, one in
  holding, same sign) on the confirmation segment;
- **segments**: the confirmation P&L cut into ``segments`` equal sub-periods.

**Many hypotheses tried once** (choice 3): ``hypotheses`` independent features
(AR(1), ``persistence``), each a single fixed strategy (lookback 1, holding 1);
the best in exploration is confirmed, and its deflation counts all of them.
With ``strength > 0`` feature 0 truly predicts next-day returns
(``meta.planted``'s construction), which gives the detection curves.
"""

import itertools
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from finresearch.core.config import load_toml
from finresearch.meta.noise import NoiseModel, generate
from finresearch.meta.planted import signal_path
from finresearch.stats.clusters import correlation_matrix, onc
from finresearch.stats.hac import newey_west_t, ordinary_t
from finresearch.stats.maxima import equivalent_trials, null_benchmark
from finresearch.stats.montecarlo import wilson_interval
from finresearch.stats.overfitting import Metric, probability_of_overfitting
from finresearch.stats.sharpe import moments, probabilistic_sharpe
from finresearch.verdict import Evidence, Thresholds, Verdict, judge

_DROPPED = 2  # "without the best two" (protocol section 5)
_CHECK = 0.99  # rates are reported with 99% Wilson intervals (decision 0018)


class BenchError(ValueError):
    """The bench cannot run as configured."""


@dataclass(frozen=True)
class Variant:
    lookback: int
    holding: int
    sign: int  # +1 follows the signal (momentum), -1 trades against it (reversal)

    def __str__(self) -> str:
        kind = "momentum" if self.sign > 0 else "reversal"
        return f"{kind} L{self.lookback} H{self.holding}"


@dataclass(frozen=True)
class BenchConfig:
    instruments: int
    days: int
    exploration: float
    confirmation: float
    embargo: int
    lookbacks: tuple[int, ...]
    holdings: tuple[int, ...]
    hypotheses: int
    persistence: float
    segments: int
    partitions: int
    restarts: int
    draws: int
    cost: float

    def __post_init__(self) -> None:
        if self.instruments < 2:  # noqa: PLR2004 - a cross-section needs two names
            raise BenchError("need at least two instruments")
        shares = (self.exploration, self.confirmation)
        if not (min(shares) > 0.0 and sum(shares) <= 1.0):
            raise BenchError(
                "exploration and confirmation must be positive shares summing to at most 1"
            )
        if not self.lookbacks or not self.holdings or min(self.lookbacks + self.holdings) < 1:
            raise BenchError("lookbacks and holdings must be positive")
        if self.embargo < max(self.holdings):
            raise BenchError("the embargo must be at least the longest holding period")
        if self.cost < 0.0:
            raise BenchError(f"cost must not be negative, got {self.cost}")


def bench_config(config: Mapping[str, Any] | None = None, **overrides: object) -> BenchConfig:
    """The bench in ``config/meta.toml`` (or ``config``); ``overrides`` replace fields."""
    cfg = load_toml("meta") if config is None else config
    w, f, m, c = cfg["world"], cfg["family"], cfg["many"], cfg["confirmation"]
    fields: dict[str, Any] = {
        "instruments": int(w["instruments"]),
        "days": int(w["days"]),
        "exploration": float(w["exploration"]),
        "confirmation": float(w["confirmation"]),
        "embargo": int(w["embargo_days"]),
        "lookbacks": tuple(int(v) for v in f["lookbacks"]),
        "holdings": tuple(int(v) for v in f["holdings"]),
        "hypotheses": int(m["hypotheses"]),
        "persistence": float(m["persistence"]),
        "segments": int(c["segments"]),
        "partitions": int(c["pbo_partitions"]),
        "restarts": int(c["onc_restarts"]),
        "draws": int(c["maximum_draws"]),
        "cost": float(cfg["costs"]["per_unit_traded"]),
    }
    fields.update(overrides)
    return BenchConfig(**fields)


def family(cfg: BenchConfig) -> list[Variant]:
    """Every lookback x holding x sign."""
    return [
        Variant(lookback, holding, sign)
        for lookback, holding, sign in itertools.product(cfg.lookbacks, cfg.holdings, (1, -1))
    ]


@dataclass(frozen=True)
class Split:
    """P&L days: exploration ``[start, exploration_end)``, confirmation ``[confirm, end)``."""

    start: int
    exploration_end: int
    confirm: int
    end: int


def split(cfg: BenchConfig, warm: int) -> Split:
    exploration_end = int(cfg.exploration * cfg.days)
    end = int((cfg.exploration + cfg.confirmation) * cfg.days)
    s = Split(warm, exploration_end, exploration_end + cfg.embargo, end)
    if not (s.start < s.exploration_end and s.confirm < s.end):
        raise BenchError(f"{cfg.days} days leave no exploration or confirmation after warm-up")
    return s


# ---------------------------------------------------------------- strategies
def rank_weights(base: Sequence[Sequence[float]], lookback: int) -> list[list[float] | None]:
    """Each day's weights from the sum of the last ``lookback`` values of ``base[i][t]``.

    Demeaned ranks scaled to gross exposure 1 (net 0); ``None`` until ``lookback``
    values exist. Ties are ranked by instrument order.
    """
    n, days = len(base), len(base[0])
    prefix: list[list[float]] = []
    for series in base:
        acc, p = 0.0, [0.0]
        for v in series:
            acc += v
            p.append(acc)
        prefix.append(p)
    centre = (n + 1) / 2.0
    gross = math.fsum(abs(r - centre) for r in range(1, n + 1))
    out: list[list[float] | None] = []
    for t in range(days):
        if t < lookback - 1:
            out.append(None)
            continue
        signal = [prefix[i][t + 1] - prefix[i][t + 1 - lookback] for i in range(n)]
        order = sorted(range(n), key=lambda i: (signal[i], i))
        w = [0.0] * n
        for rank, i in enumerate(order, start=1):
            w[i] = (rank - centre) / gross
        out.append(w)
    return out


def portfolio(weights: Sequence[list[float] | None], holding: int) -> list[list[float] | None]:
    """The average of the last ``holding`` days' weights (``None`` until all exist)."""
    out: list[list[float] | None] = []
    for t in range(len(weights)):
        window: Sequence[list[float] | None] = (
            weights[t - holding + 1 : t + 1] if t >= holding - 1 else []
        )
        live = [w for w in window if w is not None]
        if len(live) < holding:
            out.append(None)
            continue
        out.append([math.fsum(col) / holding for col in zip(*live, strict=True)])
    return out


def daily_pnl(
    returns: Sequence[Sequence[float]], positions: Sequence[list[float] | None], cost: float
) -> tuple[list[float], list[float]]:
    """Per day ``t``: gross ``sum_i W_t-1,i r_i,t``, cost ``cost x sum_i |W_t-1 - W_t-2|``.

    Zero where the positions do not exist yet.
    """
    days = len(returns[0])
    gross, costs = [0.0] * days, [0.0] * days
    for t in range(2, days):
        held, before = positions[t - 1], positions[t - 2]
        if held is None or before is None:
            continue
        gross[t] = math.fsum(w * returns[i][t] for i, w in enumerate(held))
        costs[t] = cost * math.fsum(abs(a - b) for a, b in zip(held, before, strict=True))
    return gross, costs


def cohort_returns(
    returns: Sequence[Sequence[float]],
    weights: Sequence[list[float] | None],
    holding: int,
    sign: int,
    cost: float,
    first: int,
    end: int,
) -> list[float]:
    """Non-overlapping returns: a cohort formed every ``holding`` days, held ``holding`` days.

    Cohorts are formed at the end of days ``first - 1, first - 1 + holding, ...``
    and earn the returns of the next ``holding`` days, all before ``end``; each pays
    ``cost`` on the change from the previous cohort's weights.
    """
    out: list[float] = []
    previous: list[float] | None = None
    formed = first - 1
    while formed + holding < end:
        w = weights[formed]
        if w is None:
            raise BenchError("a cohort was formed before its signal existed")
        earned = math.fsum(
            sign * wi * returns[i][formed + k]
            for i, wi in enumerate(w)
            for k in range(1, holding + 1)
        )
        traded = (
            math.fsum(abs(a) for a in w)
            if previous is None
            else math.fsum(abs(a - b) for a, b in zip(w, previous, strict=True))
        )
        out.append(earned - cost * traded)
        previous = w
        formed += holding
    return out


# ---------------------------------------------------------------- one simulated hypothesis
@dataclass(frozen=True)
class Trial:
    selected: str  # the variant (or feature) kept in exploration
    selected_index: int
    exploration_sharpe: float
    clusters: int  # ONC, describing how the trials group (not used to deflate)
    equivalent_trials: float  # independent trials with the same expected maximum
    benchmark: float  # expected best per-period Sharpe ratio under no edge (decision 0025)
    t_newey_west: float
    t_nonoverlapping: float
    dsr: float
    pbo: float
    verdict_newey_west: Verdict
    verdict_nonoverlapping: Verdict


def _sharpe(x: Sequence[float]) -> float:
    m = math.fsum(x) / len(x)
    sd = math.sqrt(math.fsum((v - m) ** 2 for v in x) / (len(x) - 1))
    return m / sd if sd > 0.0 else -math.inf


def _mean(x: Sequence[float]) -> float:
    return math.fsum(x) / len(x)


def _segments(x: Sequence[float], parts: int) -> list[float]:
    size = len(x) // parts
    if size < 1:
        raise BenchError(f"{len(x)} confirmation days cannot make {parts} segments")
    return [_mean(x[k * size : (k + 1) * size]) for k in range(parts)]


def _confirm(
    cfg: BenchConfig,
    exploration: list[list[float]],
    best: int,
    conf_net: list[float],
    conf_gross: list[float],
    conf_cost: list[float],
    cohorts: list[float],
    holding: int,
    perturbed: list[float],
    rng: random.Random,
    names: list[str],
) -> Trial:
    corr = correlation_matrix(exploration)
    clustering = onc(corr, rng, cfg.restarts)
    benchmark = null_benchmark(corr, len(exploration[0]), cfg.draws, rng)
    mo = moments(conf_net)
    dsr = probabilistic_sharpe(mo.sharpe, max(0.0, benchmark.value), mo.n, mo.skew, mo.kurt)
    equivalent = equivalent_trials(benchmark.value * math.sqrt(len(exploration[0]) - 1))
    usable = len(exploration[0]) - len(exploration[0]) % cfg.partitions
    pbo = probability_of_overfitting(
        [s[len(s) - usable :] for s in exploration], cfg.partitions, Metric.SHARPE
    ).pbo
    best_two = sorted(range(len(conf_net)), key=lambda k: conf_net[k])[-_DROPPED:]
    kept = [v for k, v in enumerate(conf_net) if k not in set(best_two)]
    segments = _segments(conf_net, cfg.segments)
    cost_doubled = _mean([g - 2.0 * c for g, c in zip(conf_gross, conf_cost, strict=True)])

    def evidence(t: float) -> Evidence:
        return Evidence(
            t=t,
            dsr=dsr,
            segments=segments,
            pbo=pbo,
            cost_doubled=cost_doubled,
            without_best_two=_mean(kept),
            perturbed=perturbed,
        )

    t_nw = newey_west_t(conf_net, holding - 1)
    t_no = ordinary_t(cohorts)
    thresholds = Thresholds.defaults()
    return Trial(
        names[best],
        best,
        _sharpe(exploration[best]),
        clustering.k,
        equivalent,
        benchmark.value,
        t_nw,
        t_no,
        dsr,
        pbo,
        judge(evidence(t_nw), thresholds),
        judge(evidence(t_no), thresholds),
    )


def run_family(cfg: BenchConfig, model: NoiseModel, rng: random.Random) -> Trial:
    """One researcher exploring the momentum/reversal family on a noise world (choice 1)."""
    returns = generate(model, cfg.instruments, cfg.days, rng)
    variants = family(cfg)
    sp = split(cfg, max(cfg.lookbacks) + max(cfg.holdings))
    weights = {lb: rank_weights(returns, lb) for lb in cfg.lookbacks}
    pnl: dict[tuple[int, int], tuple[list[float], list[float]]] = {}
    for lb, h in itertools.product(cfg.lookbacks, cfg.holdings):
        pnl[(lb, h)] = daily_pnl(returns, portfolio(weights[lb], h), cfg.cost)

    def net(v: Variant, lo: int, hi: int) -> list[float]:
        gross, costs = pnl[(v.lookback, v.holding)]
        return [v.sign * gross[t] - costs[t] for t in range(lo, hi)]

    exploration = [net(v, sp.start, sp.exploration_end) for v in variants]
    best = max(range(len(variants)), key=lambda k: (_sharpe(exploration[k]), -k))
    v = variants[best]
    gross, costs = pnl[(v.lookback, v.holding)]
    window = range(sp.confirm, sp.end)
    li, hi_ = cfg.lookbacks.index(v.lookback), cfg.holdings.index(v.holding)
    neighbours = [
        Variant(cfg.lookbacks[j], v.holding, v.sign)
        for j in (li - 1, li + 1)
        if 0 <= j < len(cfg.lookbacks)
    ] + [
        Variant(v.lookback, cfg.holdings[j], v.sign)
        for j in (hi_ - 1, hi_ + 1)
        if 0 <= j < len(cfg.holdings)
    ]
    return _confirm(
        cfg,
        exploration,
        best,
        net(v, sp.confirm, sp.end),
        [v.sign * gross[t] for t in window],
        [costs[t] for t in window],
        cohort_returns(
            returns, weights[v.lookback], v.holding, v.sign, cfg.cost, sp.confirm, sp.end
        ),
        v.holding,
        [_mean(net(n, sp.confirm, sp.end)) for n in neighbours],
        rng,
        [str(x) for x in variants],
    )


def run_many(
    cfg: BenchConfig, model: NoiseModel, rng: random.Random, strength: float = 0.0
) -> Trial:
    """Many independent hypotheses tried once each; the best is confirmed (choice 3).

    With ``strength > 0``, feature 0 predicts the next day's return by
    ``strength`` times the instrument's volatility (detection curves, choice 4).
    """
    returns = generate(model, cfg.instruments, cfg.days, rng)
    features = [
        [signal_path(cfg.days, cfg.persistence, rng) for _ in range(cfg.instruments)]
        for _ in range(cfg.hypotheses)
    ]
    if strength:
        for i, r in enumerate(returns):
            m = _mean(r)
            sd = math.sqrt(math.fsum((x - m) ** 2 for x in r) / (len(r) - 1))
            for t in range(1, cfg.days):
                r[t] += strength * sd * features[0][i][t - 1]
    sp = split(cfg, 2)
    weights = [rank_weights(f, 1) for f in features]
    pnl = [daily_pnl(returns, portfolio(w, 1), cfg.cost) for w in weights]
    exploration = [[g[t] - c[t] for t in range(sp.start, sp.exploration_end)] for g, c in pnl]
    best = max(range(cfg.hypotheses), key=lambda k: (_sharpe(exploration[k]), -k))
    gross, costs = pnl[best]
    window = range(sp.confirm, sp.end)
    return _confirm(
        cfg,
        exploration,
        best,
        [gross[t] - costs[t] for t in window],
        [gross[t] for t in window],
        [costs[t] for t in window],
        cohort_returns(returns, weights[best], 1, 1, cfg.cost, sp.confirm, sp.end),
        1,
        [],
        rng,
        [f"feature {k}" for k in range(cfg.hypotheses)],
    )


# ---------------------------------------------------------------- rates
@dataclass(frozen=True)
class Rate:
    hits: int
    n: int

    @property
    def share(self) -> float:
        return self.hits / self.n

    @property
    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.hits, self.n, _CHECK)


def rates(trials: Sequence[Trial]) -> dict[str, Rate]:
    """How often each gate, and the whole pipeline, said yes."""
    if not trials:
        raise BenchError("no trials")
    th = Thresholds.defaults()
    n = len(trials)
    return {
        "t (Newey-West)": Rate(sum(1 for r in trials if r.t_newey_west >= th.t), n),
        "t (non-overlapping)": Rate(sum(1 for r in trials if r.t_nonoverlapping >= th.t), n),
        "deflated Sharpe": Rate(sum(1 for r in trials if r.dsr >= th.dsr_min), n),
        "pipeline (Newey-West)": Rate(sum(1 for r in trials if r.verdict_newey_west.passed), n),
        "pipeline (non-overlapping)": Rate(
            sum(1 for r in trials if r.verdict_nonoverlapping.passed), n
        ),
    }
