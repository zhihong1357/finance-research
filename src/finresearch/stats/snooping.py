"""Tests that know how many strategies were tried (roadmap step 3.4).

When the best of ``K`` strategies is tested as if it were the only one, chance
alone makes it look good. Both tests here resample all ``K`` strategies'
performance series together with one set of stationary-bootstrap blocks
(``stats.bootstrap``), so the dependence across strategies and over time is
kept.

- **Reality Check** (White 2000, section 2.3 and pp. 1109-1110): is the best
  strategy better than the benchmark at all? ``V = max_k sqrt(n) mean_k``;
  each resample gives ``V*_i = max_k sqrt(n) (mean*_k,i - mean_k)``; with
  ``M`` the number of ``V*`` at or below ``V`` among ``N`` resamples, the
  p-value is ``1 - M / N``.
- **StepM** (Romano & Wolf 2005; Algorithms 3.1 and 3.2 of the 2003 working
  paper, basic statistics): which strategies beat the benchmark, controlling
  the familywise error rate at ``alpha``. With ``w_k`` the mean performance, the
  first step rejects every ``k`` with ``w_k > c``, where ``c`` is the ``1 - alpha``
  quantile of ``max_k (w*_k - w_k)`` over the resamples; each further step
  recomputes ``c`` over the strategies not yet rejected, until a step rejects
  nothing. The quantile is the paper's (equation 10): the smallest value whose
  empirical distribution function reaches ``1 - alpha``.

- **SPA** (Hansen 2005, *Journal of Business & Economic Statistics* 23(4),
  365-380): the Reality Check's question, with two changes that make it less
  sensitive to poor and irrelevant strategies. (1) Each mean is divided by its
  own standard error, ``T = max(0, max_k sqrt(n) mean_k / omega_k)`` (p. 368),
  so a volatile strategy no longer dominates. (2) The null distribution keeps
  the strategies that are clearly worse than the benchmark where they are
  instead of moving them up to the boundary: resample ``b`` gives
  ``T*_b = max(0, max_k sqrt(n) (mean*_k,b - g(mean_k)) / omega_k)`` and the
  p-value is the share of ``T*_b > T`` (strictly, p. 372), with three choices of
  ``g`` (p. 372): ``g_l(x) = max(0, x)`` (a liberal bound), ``g_c(x) = x`` when
  ``sqrt(n) x / omega >= -sqrt(2 log log n)`` and 0 otherwise (the consistent
  test the paper recommends), and ``g_u(x) = x`` (the least favourable
  configuration, a conservative bound: the Reality Check, studentized).
  ``g_l >= g_c >= g_u`` everywhere, so the three p-values are always in that
  order. ``omega_k^2`` is the stationary bootstrap's own variance of
  ``sqrt(n)`` times the mean, which the paper recommends over a bootstrap
  estimate of it (p. 372, from Politis & Romano 1994); the same ``omega_k`` is
  used for ``T`` and every ``T*_b``.

``performance[k]`` is strategy ``k``'s performance relative to the benchmark
at each time (for strategies judged against zero, simply its returns). The
Reality Check and StepM use the basic statistic, the mean, so a volatile
strategy weighs more: scale the series to a common volatility first when that
is not wanted (SPA does this itself). The studentized StepM (the paper's
section 4) is not implemented: it needs a kernel variance estimate inside every
resample.

The bootstrap draws are separated from the decisions (``reality_check_p_value``,
``stepdown`` and ``spa_p_values`` take them as numbers), so every decision rule
can be checked by hand.
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from finresearch.stats.bootstrap import stationary_blocks

_MIN_STRATEGIES = 1
_SLACK = 1e-9  # ceil((1 - alpha) J) must not round up past an exact integer
_MIN_SPA_OBSERVATIONS = 3  # log log n must be positive


class SnoopingError(ValueError):
    """The strategies or the draws cannot be tested as asked."""


@dataclass(frozen=True)
class RealityCheck:
    statistic: float  # V = max_k sqrt(n) mean_k
    p_value: float
    best: int  # the strategy with the largest mean


@dataclass(frozen=True)
class StepdownResult:
    rejected: tuple[int, ...]  # strategies that beat the benchmark, in the order rejected
    steps: tuple[tuple[int, ...], ...]  # which were rejected at each step
    critical_values: tuple[float, ...]  # c at each step (the last one rejected nothing)


@dataclass(frozen=True)
class SuperiorPredictiveAbility:
    statistic: float  # T = max(0, max_k sqrt(n) mean_k / omega_k)
    lower: float  # p-value with g_l: every poor strategy kept where it is (liberal)
    consistent: float  # p-value with g_c: only the clearly poor ones kept (Hansen's test)
    upper: float  # p-value with g_u: every strategy moved to the boundary (conservative)
    best: int  # the strategy with the largest studentized mean


def _check(performance: Sequence[Sequence[float]]) -> int:
    if len(performance) < _MIN_STRATEGIES:
        raise SnoopingError("need at least one strategy")
    n = len(performance[0])
    if n < 2 or any(len(s) != n for s in performance):  # noqa: PLR2004 - a mean and its spread
        raise SnoopingError("every strategy needs the same number (at least 2) of observations")
    if not all(math.isfinite(v) for s in performance for v in s):
        raise SnoopingError("performance must be finite")
    return n


def resampled_means(
    performance: Sequence[Sequence[float]],
    mean_block: float,
    replications: int,
    rng: random.Random,
) -> tuple[list[float], list[list[float]]]:
    """The strategies' means, and ``replications`` resamples of them with shared blocks.

    Returns ``(means, draws)`` with ``draws[j][k]`` strategy ``k``'s mean in resample ``j``.
    """
    n = _check(performance)
    if replications < 1:
        raise SnoopingError(f"need at least one replication, got {replications}")
    means = [math.fsum(s) / n for s in performance]
    prefixes: list[list[float]] = []
    for s, m in zip(performance, means, strict=True):
        prefix = [0.0]
        for v in s:  # centred, so the running totals stay small
            prefix.append(prefix[-1] + (v - m))
        prefixes.append(prefix)
    draws: list[list[float]] = []
    for _ in range(replications):
        blocks = stationary_blocks(n, mean_block, rng)
        row: list[float] = []
        for prefix, m in zip(prefixes, means, strict=True):
            total = prefix[n]
            sums: list[float] = []
            for start, length in blocks:
                end = start + length
                if end <= n:
                    sums.append(prefix[end] - prefix[start])
                else:
                    sums.append(total - prefix[start] + prefix[end - n])
            row.append(m + math.fsum(sums) / n)
        draws.append(row)
    return means, draws


def stationary_variance(series: Sequence[float], mean_block: float) -> float:
    """``omega^2``: the variance of ``sqrt(n)`` times the stationary bootstrap's resampled mean.

    Politis & Romano (1994) as Hansen (2005, p. 372) writes it, with ``q = 1 / mean_block``:
    ``gamma_0 + 2 sum_{i=1}^{n-1} kappa(n, i) gamma_i``, where ``kappa(n, i) = ((n - i) / n)
    (1 - q)^i + (i / n)(1 - q)^(n - i)`` and ``gamma_i = (1 / n) sum_j (d_j - mean)(d_{j+i} -
    mean)``. Pairing lag ``i`` with lag ``n - i`` turns this into the circular autocovariances
    ``R_i = (1 / n) sum_j x_j x_{(j+i) mod n}``: ``R_0 + 2 sum_{i=1}^{n-1} (1 - i / n)(1 - q)^i
    R_i``. With ``A_j = sum_i (1 - q)^i x_{j+i}`` and ``B_j = sum_i i (1 - q)^i x_{j+i}`` (lags 1
    to ``n - 1``, indices modulo ``n``) that is ``(1 / n) sum_j x_j (x_j + 2 (A_j - B_j / n))``,
    and both sums follow from their value at ``j + 1``, so the whole costs ``O(n)``, not
    ``O(n^2)``. ``mean_block = 1`` (no blocks) gives the plain variance ``gamma_0``.
    """
    n = _check([series])
    if not (mean_block >= 1.0 and math.isfinite(mean_block)):
        raise SnoopingError(f"mean block length must be at least 1, got {mean_block}")
    mean = math.fsum(series) / n
    x = [v - mean for v in series]
    stay = 1.0 - 1.0 / mean_block
    wrap = stay**n  # the weight of a full turn: what leaves the window at the far end
    a = b = 0.0  # A_0, B_0 directly
    weight = 1.0
    for i in range(1, n):
        weight *= stay
        a += weight * x[i]
        b += i * weight * x[i]
    terms = [x[0] * (x[0] + 2.0 * (a - b / n))]
    for j in range(n - 1, 0, -1):  # A_j and B_j from A_{j+1} and B_{j+1} (A_n is A_0)
        a = stay * (x[(j + 1) % n] + a) - wrap * x[j]
        b = stay * b + a - (n - 1) * wrap * x[j]
        terms.append(x[j] * (x[j] + 2.0 * (a - b / n)))
    variance = math.fsum(terms) / n
    if not variance > 0.0:
        raise SnoopingError("the series does not vary, so its mean has no standard error")
    return variance


def reality_check_p_value(
    means: Sequence[float], draws: Sequence[Sequence[float]], n: int
) -> RealityCheck:
    """White's p-value from the means and the resampled means (``n`` observations each)."""
    if not draws or any(len(row) != len(means) for row in draws):
        raise SnoopingError("every draw needs one mean per strategy")
    root = math.sqrt(n)
    statistic = root * max(means)
    v_star = [root * max(d - m for d, m in zip(row, means, strict=True)) for row in draws]
    at_or_below = sum(1 for v in v_star if v <= statistic)
    return RealityCheck(statistic, 1.0 - at_or_below / len(v_star), means.index(max(means)))


def _quantile(values: Sequence[float], level: float) -> float:
    """``inf{x : F(x) >= level}`` for the empirical distribution of ``values``."""
    s = sorted(values)
    return s[max(0, math.ceil(level * len(s) - _SLACK) - 1)]


def stepdown(
    means: Sequence[float], draws: Sequence[Sequence[float]], alpha: float
) -> StepdownResult:
    """Romano & Wolf's StepM decisions from the means and the resampled means."""
    if not 0.0 < alpha < 1.0:
        raise SnoopingError(f"alpha must be strictly between 0 and 1, got {alpha}")
    if not draws or any(len(row) != len(means) for row in draws):
        raise SnoopingError("every draw needs one mean per strategy")
    order = sorted(range(len(means)), key=lambda k: (-means[k], k))
    centred = [[d - m for d, m in zip(row, means, strict=True)] for row in draws]
    remaining: list[int] = list(order)
    rejected: list[int] = []
    steps: list[tuple[int, ...]] = []
    criticals: list[float] = []
    while remaining:
        c = _quantile([max(row[k] for k in remaining) for row in centred], 1.0 - alpha)
        criticals.append(c)
        now = tuple(k for k in remaining if means[k] - c > 0.0)
        if not now:
            break
        steps.append(now)
        rejected.extend(now)
        remaining = [k for k in remaining if k not in now]
    return StepdownResult(tuple(rejected), tuple(steps), tuple(criticals))


def spa_p_values(
    means: Sequence[float],
    draws: Sequence[Sequence[float]],
    n: int,
    omegas: Sequence[float],
) -> SuperiorPredictiveAbility:
    """Hansen's three SPA p-values from the means, the resampled means and every ``omega_k``."""
    if not draws or any(len(row) != len(means) for row in draws):
        raise SnoopingError("every draw needs one mean per strategy")
    if len(omegas) != len(means) or not all(w > 0.0 and math.isfinite(w) for w in omegas):
        raise SnoopingError("need one positive, finite standard error per strategy")
    if n < _MIN_SPA_OBSERVATIONS:
        raise SnoopingError(f"need at least {_MIN_SPA_OBSERVATIONS} observations, got {n}")
    scale = [math.sqrt(n) / w for w in omegas]
    t = [m * c for m, c in zip(means, scale, strict=True)]
    statistic = max(0.0, *t)
    floor = -math.sqrt(2.0 * math.log(math.log(n)))
    shifts = (
        [max(0.0, m) for m in means],  # g_l
        [m if tk >= floor else 0.0 for m, tk in zip(means, t, strict=True)],  # g_c
        list(means),  # g_u
    )
    p: list[float] = []
    for g in shifts:
        # T*_b = max(0, x) with T >= 0, so T*_b > T exactly when x > T
        above = sum(
            1
            for row in draws
            if max((d - s) * c for d, s, c in zip(row, g, scale, strict=True)) > statistic
        )
        p.append(above / len(draws))
    return SuperiorPredictiveAbility(statistic, p[0], p[1], p[2], t.index(max(t)))


def reality_check(
    performance: Sequence[Sequence[float]],
    mean_block: float,
    replications: int,
    rng: random.Random,
) -> RealityCheck:
    """White's Bootstrap Reality Check: does the best strategy beat the benchmark?"""
    means, draws = resampled_means(performance, mean_block, replications, rng)
    return reality_check_p_value(means, draws, len(performance[0]))


def stepm(
    performance: Sequence[Sequence[float]],
    alpha: float,
    mean_block: float,
    replications: int,
    rng: random.Random,
) -> StepdownResult:
    """Romano & Wolf's StepM: which strategies beat the benchmark (familywise error ``alpha``)."""
    means, draws = resampled_means(performance, mean_block, replications, rng)
    return stepdown(means, draws, alpha)


def spa(
    performance: Sequence[Sequence[float]],
    mean_block: float,
    replications: int,
    rng: random.Random,
) -> SuperiorPredictiveAbility:
    """Hansen's test for superior predictive ability: does the best strategy beat the benchmark?"""
    means, draws = resampled_means(performance, mean_block, replications, rng)
    omegas = [math.sqrt(stationary_variance(s, mean_block)) for s in performance]
    return spa_p_values(means, draws, len(performance[0]), omegas)
