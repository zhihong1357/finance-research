"""The researcher who bends the protocol: how much each bend inflates false positives.

An extension of the meta-research bench (step 3.12), prompted by the red-team
review of 2026-11-19 (its point 12: the bench measured only a researcher who
follows the rules). On the same noise world as ``bench.run_family``, the honest
researcher keeps the variant with the best exploration Sharpe ratio and
reports its non-overlapping t on the whole confirmation segment. Each cheat
changes one thing and reports what it likes best:

- **direction**: chooses the sign after seeing the confirmation, i.e. reports
  the better of the kept variant and its mirror image (same lookback and
  holding, opposite sign);
- **peek k**: runs the ``k`` best exploration variants on the confirmation
  segment and reports the best of them (unrecorded confirmation runs);
- **method**: reports the larger of the Newey-West and the non-overlapping t;
- **sub-period**: reports the best of several windows of the confirmation
  segment (``windows``, as shares of it; the whole segment must be one);
- **threshold**: the honest t against the protocol's floor (``t_floor``, the
  exception "with a clear mechanism") instead of the default;
- **unlogged**: records only ``logged_variants`` of the family's trials, so the
  deflated Sharpe ratio's no-edge benchmark counts fewer trials;
- **combined**: direction, peek 3 and method at once.

Every claim is the full evidence of what is reported (its segments, costs,
neighbours, and the deflated Sharpe ratio against the family's benchmark),
judged by ``verdict.judge``. By construction no cheat's t is below the honest
one on the same world, which the tests check world by world.

The honest numbers equal ``bench.run_family``'s on the same seed: the world and
the family statistics draw from ``rng`` in the same order. The unlogged cheat's
benchmark needs its own draws, from ``cheat_rng``.

The same cheats run on the many-hypotheses world too (``run_cheats_many``):
twenty independent features, each a single fixed strategy, the best of them
confirmed. A single fixed strategy has no parameters to choose, so there PBO
does not apply; ``rates`` reports the pipeline without it.

Run ``python -m finresearch.meta.cheats OUT.json [family|many]``; sizes in
``config/meta.toml``'s ``[cheats]``. Synthetic data only, so the results may be
published.
"""

import json
import random
import sys
from collections.abc import Callable, Hashable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any, Protocol

from finresearch.core.config import load_toml
from finresearch.meta.bench import (
    BenchConfig,
    BenchError,
    Confirmation,
    FamilyStats,
    Rate,
    Split,
    bench_config,
    evidence,
    family_stats,
    family_world,
    many_world,
)
from finresearch.meta.noise import NoiseModel, noise_model
from finresearch.stats.clusters import correlation_matrix
from finresearch.stats.maxima import null_benchmark
from finresearch.verdict import Outcome, Thresholds, judge

_SEED_BASE = {"family": 50_000_000, "many": 60_000_000}  # task i: base + i (apart from the dry run)
_COMBINED_PEEK = 3


@dataclass(frozen=True)
class CheatsConfig:
    peeks: tuple[int, ...]
    many_peeks: tuple[int, ...]  # the same, among the many hypotheses
    windows: tuple[tuple[float, float], ...]
    t_floor: float
    logged_variants: int
    levels: tuple[float, ...]
    runs: int
    workers: int

    def __post_init__(self) -> None:
        if not self.peeks or not self.many_peeks or min(self.peeks + self.many_peeks) < 1:
            raise BenchError("peeks must be positive")
        if (0.0, 1.0) not in self.windows:
            raise BenchError("the whole confirmation segment must be one of the windows")
        if not all(0.0 <= a < b <= 1.0 for a, b in self.windows):
            raise BenchError("windows are shares (a, b) of the segment with 0 <= a < b <= 1")
        if self.logged_variants < 2:  # noqa: PLR2004 - a benchmark needs two trials
            raise BenchError("at least two variants must be logged")


def cheats_config(config: Mapping[str, Any] | None = None) -> CheatsConfig:
    """``[cheats]`` of ``config/meta.toml`` (or ``config``), with the protocol's ``t_floor``."""
    c = (load_toml("meta") if config is None else config)["cheats"]
    return CheatsConfig(
        peeks=tuple(int(k) for k in c["peeks"]),
        many_peeks=tuple(int(k) for k in c["many_peeks"]),
        windows=tuple((float(a), float(b)) for a, b in c["windows"]),
        t_floor=float(load_toml("protocol")["thresholds"]["t_floor"]),
        logged_variants=int(c["logged_variants"]),
        levels=tuple(float(v) for v in c["levels"]),
        runs=int(c["runs"]),
        workers=int(c["workers"]),
    )


@dataclass(frozen=True)
class Claim:
    variant: str
    t: float
    dsr: float
    passed: bool  # the whole verdict on the reported evidence
    # The same claim judged with the deflated Sharpe gate fixed as the independent review
    # proposes: the confirmation is a fresh test, so its probabilistic Sharpe ratio is taken
    # against 0 instead of against the family's expected best (no second deflation).
    dsr_fresh: float
    passed_fresh: bool
    failed_fresh: tuple[str, ...]  # the checks that failed in that verdict


def window_bounds(confirm: int, end: int, share: tuple[float, float]) -> tuple[int, int]:
    """Days ``[lo, hi)`` of the confirmation segment ``[confirm, end)`` covering ``share``."""
    span = end - confirm
    return confirm + int(share[0] * span), confirm + int(share[1] * span)


def logged_trials(best: int, n: int, logged: int) -> list[int]:
    """The trials an unlogging researcher records: the kept one, then the first others."""
    return [best, *[k for k in range(n) if k != best][: max(0, logged - 1)]]


class _World[S: Hashable](Protocol):
    """What the cheats need of a world: ``FamilyWorld`` and ``ManyWorld`` both have it."""

    @property
    def cfg(self) -> BenchConfig: ...
    @property
    def bounds(self) -> Split: ...
    @property
    def exploration(self) -> list[list[float]]: ...
    @property
    def best(self) -> int: ...
    def ranked(self) -> list[int]: ...
    def mirror(self, s: S, /) -> S: ...
    def confirmation(
        self, s: S, lo: int | None = None, hi: int | None = None, /
    ) -> Confirmation: ...


class _Claims[S: Hashable]:
    """Claims about one world, sharing the family statistics and caching each run."""

    def __init__(self, world: _World[S], stats: FamilyStats) -> None:
        self.world, self.stats = world, stats
        self.defaults = Thresholds.defaults()
        self._runs: dict[tuple[S, int, int], Confirmation] = {}

    def run(self, s: S, lo: int | None = None, hi: int | None = None) -> Confirmation:
        b = self.world.bounds
        key = (s, b.confirm if lo is None else lo, b.end if hi is None else hi)
        if key not in self._runs:
            self._runs[key] = self.world.confirmation(s, key[1], key[2])
        return self._runs[key]

    def claim(
        self,
        s: S,
        c: Confirmation,
        t: float,
        thresholds: Thresholds | None = None,
        benchmark: float | None = None,
    ) -> Claim:
        bm = self.stats.benchmark if benchmark is None else benchmark
        th = thresholds or self.defaults
        e = evidence(self.world.cfg, bm, self.stats.pbo, c, t)
        fresh = evidence(self.world.cfg, 0.0, self.stats.pbo, c, t)
        verdict = judge(fresh, th)
        failed = tuple(k.value for k, o in verdict.outcomes.items() if o is Outcome.FAILED)
        return Claim(
            str(s), t, e.dsr, judge(e, th).passed, fresh.dsr, verdict.passed, tuple(sorted(failed))
        )

    def honest_t(self, s: S) -> Claim:
        c = self.run(s)
        return self.claim(s, c, c.t_nonoverlapping)

    def either_t(self, s: S) -> Claim:
        c = self.run(s)
        return self.claim(s, c, max(c.t_newey_west, c.t_nonoverlapping))


def _best(claims: Sequence[Claim]) -> Claim:
    return max(claims, key=lambda c: c.t)  # the first of equals


def _cheat_claims[S: Hashable](
    world: _World[S],
    strategies: Sequence[S],
    stats: FamilyStats,
    cc: CheatsConfig,
    peeks: Sequence[int],
    cheat_rng: random.Random,
) -> dict[str, Claim]:
    k = _Claims(world, stats)
    best = world.best
    kept = strategies[best]
    out: dict[str, Claim] = {"honest": k.honest_t(kept)}
    out["direction"] = _best([out["honest"], k.honest_t(world.mirror(kept))])
    order = world.ranked()
    for p in peeks:
        out[f"peek {p}"] = _best([k.honest_t(strategies[i]) for i in order[:p]])
    out["method"] = k.either_t(kept)
    windows: list[Claim] = []
    for share in cc.windows:
        lo, hi = window_bounds(world.bounds.confirm, world.bounds.end, share)
        c = k.run(kept, lo, hi)
        windows.append(k.claim(kept, c, c.t_nonoverlapping))
    out["sub-period"] = _best(windows)
    relaxed = Thresholds(cc.t_floor, k.defaults.dsr_min, k.defaults.pbo_max)
    honest = k.run(kept)
    out["threshold"] = k.claim(kept, honest, honest.t_nonoverlapping, thresholds=relaxed)
    explored = world.exploration
    logged = [explored[i] for i in logged_trials(best, len(strategies), cc.logged_variants)]
    fewer = null_benchmark(correlation_matrix(logged), len(logged[0]), world.cfg.draws, cheat_rng)
    out["unlogged"] = k.claim(kept, honest, honest.t_nonoverlapping, benchmark=fewer.value)
    top = [strategies[i] for i in order[:_COMBINED_PEEK]]
    out["combined"] = _best([k.either_t(s) for s in [*top, *(world.mirror(s) for s in top)]])
    return out


def run_cheats(
    cfg: BenchConfig,
    cc: CheatsConfig,
    model: NoiseModel,
    rng: random.Random,
    cheat_rng: random.Random,
) -> dict[str, Claim]:
    """The honest claim and every cheat's on one noise world of the family."""
    world = family_world(cfg, model, rng)
    stats = family_stats(cfg, world.exploration, rng)
    return _cheat_claims(world, world.variants, stats, cc, cc.peeks, cheat_rng)


def run_cheats_many(
    cfg: BenchConfig,
    cc: CheatsConfig,
    model: NoiseModel,
    rng: random.Random,
    cheat_rng: random.Random,
) -> dict[str, Claim]:
    """The same on a world of the many hypotheses, each a single fixed strategy.

    The honest claim equals ``bench.run_many``'s. A single fixed strategy has no
    parameters to choose, so the faithful verdict leaves PBO out: see
    ``rates``' "no pbo" column.
    """
    world = many_world(cfg, model, rng)
    stats = family_stats(cfg, world.exploration, rng)
    return _cheat_claims(world, world.features, stats, cc, cc.many_peeks, cheat_rng)


# ---------------------------------------------------------------- the run and its summary
def run_task(seed: int, mode: str = "family") -> dict[str, Any]:
    """One noise world without costs (decision 0018 compares gates against their nominal
    level without costs), as a flat record. ``mode`` is "family" or "many"."""
    cfg = bench_config(cost=0.0)
    rng = random.Random(seed)  # noqa: S311 - a simulation's seeded stream, not a secret
    cheat_rng = random.Random(f"cheats-{seed}")  # noqa: S311
    run = run_cheats if mode == "family" else run_cheats_many
    claims = run(cfg, cheats_config(), noise_model(), rng, cheat_rng)
    return {"seed": seed, "mode": mode, "claims": {k: asdict(c) for k, c in claims.items()}}


def _t_at_least(level: float) -> Callable[[Mapping[str, Any]], bool]:
    return lambda c: c["t"] >= level


def rates(
    rows: Sequence[Mapping[str, Any]], levels: Sequence[float], dsr_min: float
) -> dict[str, dict[str, Rate]]:
    """For every claim: how often t reached each level, the deflated Sharpe gate passed,
    and the whole verdict passed."""
    if not rows:
        raise BenchError("no rows")
    n = len(rows)
    out: dict[str, dict[str, Rate]] = {}
    for name in rows[0]["claims"]:
        claims = [r["claims"][name] for r in rows]
        gates: list[tuple[str, Callable[[Mapping[str, Any]], bool]]] = [
            *((f"t >= {lv:g}", _t_at_least(lv)) for lv in levels),
            (f"dsr >= {dsr_min:g}", lambda c: c["dsr"] >= dsr_min),
            ("pipeline", lambda c: bool(c["passed"])),
            (f"fresh dsr >= {dsr_min:g}", lambda c: c["dsr_fresh"] >= dsr_min),
            ("pipeline (fresh dsr)", lambda c: bool(c["passed_fresh"])),
            # a single fixed strategy has no parameters to choose: PBO does not apply
            ("pipeline (fresh dsr, no pbo)", lambda c: set(c["failed_fresh"]) <= {"pbo"}),
        ]
        out[name] = {g: Rate(sum(1 for c in claims if test(c)), n) for g, test in gates}
    return out


def blockers(rows: Sequence[Mapping[str, Any]], name: str, t: float) -> dict[str, int]:
    """Among ``name``'s claims with t at least ``t``, how often each check failed when the
    deflated Sharpe gate is fixed (a claim can fail several)."""
    out: dict[str, int] = {}
    for r in rows:
        c = r["claims"][name]
        if c["t"] >= t:
            for check in c["failed_fresh"]:
                out[check] = out.get(check, 0) + 1
    return dict(sorted(out.items()))


def summarize(rows: Sequence[Mapping[str, Any]], cc: CheatsConfig) -> str:
    """The table the report quotes: each claim's rates and its inflation over honest."""
    th = Thresholds.defaults()
    table = rates(rows, cc.levels, th.dsr_min)
    honest = table["honest"]
    lines = [f"{len(rows)} noise worlds, no costs"]
    for name, gates in table.items():
        parts: list[str] = []
        for g, r in gates.items():
            low, high = r.interval
            ratio = r.hits / honest[g].hits if honest[g].hits else float("nan")
            parts.append(f"{g}: {r.hits} ({100 * low:.2f}-{100 * high:.2f}%) x{ratio:.2f}")
        lines.append(f"{name}: " + "; ".join(parts))
    lines.append(f"checks failing among claims with t >= {th.t:g}, deflated Sharpe gate fixed:")
    for name in table:
        reached = sum(1 for r in rows if r["claims"][name]["t"] >= th.t)
        lines.append(f"  {name} ({reached} reached t): {blockers(rows, name, th.t)}")
    return "\n".join(lines)


def main(argv: Sequence[str]) -> None:
    """Run every task in parallel, write the rows to ``argv[0]`` and print the summary.

    ``argv[1]``, if given, is the mode: "family" (the default) or "many".
    """
    cc = cheats_config()
    mode = argv[1] if len(argv) > 1 else "family"
    if mode not in _SEED_BASE:
        raise BenchError(f"mode must be family or many, got {mode!r}")
    seeds = [_SEED_BASE[mode] + i for i in range(cc.runs)]
    with ProcessPoolExecutor(cc.workers) as pool:
        rows = list(pool.map(partial(run_task, mode=mode), seeds, chunksize=4))
    with open(argv[0], "w", encoding="utf-8") as fh:  # noqa: PTH123 - a path given by the caller
        json.dump(rows, fh)
    print(summarize(rows, cc))  # noqa: T201 - this is a command-line tool


if __name__ == "__main__":
    main(sys.argv[1:])
