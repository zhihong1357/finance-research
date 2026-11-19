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

Run ``python -m finresearch.meta.cheats OUT.json``; sizes in ``config/meta.toml``'s
``[cheats]``. Synthetic data only, so the results may be published.
"""

import json
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from finresearch.core.config import load_toml
from finresearch.meta.bench import (
    BenchConfig,
    BenchError,
    Confirmation,
    FamilyStats,
    FamilyWorld,
    Rate,
    Variant,
    bench_config,
    evidence,
    family_stats,
    family_world,
)
from finresearch.meta.noise import NoiseModel, noise_model
from finresearch.stats.clusters import correlation_matrix
from finresearch.stats.maxima import null_benchmark
from finresearch.verdict import Thresholds, judge

_SEED_BASE = 50_000_000  # task i has seed _SEED_BASE + i (apart from the dry run's seeds)
_COMBINED_PEEK = 3


@dataclass(frozen=True)
class CheatsConfig:
    peeks: tuple[int, ...]
    windows: tuple[tuple[float, float], ...]
    t_floor: float
    logged_variants: int
    levels: tuple[float, ...]
    runs: int
    workers: int

    def __post_init__(self) -> None:
        if not self.peeks or min(self.peeks) < 1:
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


def window_bounds(confirm: int, end: int, share: tuple[float, float]) -> tuple[int, int]:
    """Days ``[lo, hi)`` of the confirmation segment ``[confirm, end)`` covering ``share``."""
    span = end - confirm
    return confirm + int(share[0] * span), confirm + int(share[1] * span)


def logged_trials(best: int, n: int, logged: int) -> list[int]:
    """The trials an unlogging researcher records: the kept one, then the first others."""
    return [best, *[k for k in range(n) if k != best][: max(0, logged - 1)]]


class _Claims:
    """Claims about one world, sharing the family statistics and caching each run."""

    def __init__(self, world: FamilyWorld, stats: FamilyStats) -> None:
        self.world, self.stats = world, stats
        self.defaults = Thresholds.defaults()
        self._runs: dict[tuple[Variant, int, int], Confirmation] = {}

    def run(self, v: Variant, lo: int | None = None, hi: int | None = None) -> Confirmation:
        b = self.world.bounds
        key = (v, b.confirm if lo is None else lo, b.end if hi is None else hi)
        if key not in self._runs:
            self._runs[key] = self.world.confirmation(v, key[1], key[2])
        return self._runs[key]

    def claim(
        self,
        v: Variant,
        c: Confirmation,
        t: float,
        thresholds: Thresholds | None = None,
        benchmark: float | None = None,
    ) -> Claim:
        bm = self.stats.benchmark if benchmark is None else benchmark
        e = evidence(self.world.cfg, bm, self.stats.pbo, c, t)
        return Claim(str(v), t, e.dsr, judge(e, thresholds or self.defaults).passed)

    def honest_t(self, v: Variant) -> Claim:
        c = self.run(v)
        return self.claim(v, c, c.t_nonoverlapping)

    def either_t(self, v: Variant) -> Claim:
        c = self.run(v)
        return self.claim(v, c, max(c.t_newey_west, c.t_nonoverlapping))


def _best(claims: Sequence[Claim]) -> Claim:
    return max(claims, key=lambda c: c.t)  # the first of equals


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
    k = _Claims(world, stats)
    variants, best = world.variants, world.best
    kept = variants[best]

    def mirror(v: Variant) -> Variant:
        return Variant(v.lookback, v.holding, -v.sign)

    out: dict[str, Claim] = {"honest": k.honest_t(kept)}
    out["direction"] = _best([out["honest"], k.honest_t(mirror(kept))])
    order = world.ranked()
    for p in cc.peeks:
        out[f"peek {p}"] = _best([k.honest_t(variants[i]) for i in order[:p]])
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
    logged = [world.exploration[i] for i in logged_trials(best, len(variants), cc.logged_variants)]
    fewer = null_benchmark(correlation_matrix(logged), len(logged[0]), cfg.draws, cheat_rng)
    out["unlogged"] = k.claim(kept, honest, honest.t_nonoverlapping, benchmark=fewer.value)
    top = [variants[i] for i in order[:_COMBINED_PEEK]]
    out["combined"] = _best([k.either_t(v) for v in top + [mirror(v) for v in top]])
    return out


# ---------------------------------------------------------------- the run and its summary
def run_task(seed: int) -> dict[str, Any]:
    """One noise world without costs (decision 0018 compares gates against their nominal
    level without costs), as a flat record."""
    cfg = bench_config(cost=0.0)
    rng = random.Random(seed)  # noqa: S311 - a simulation's seeded stream, not a secret
    cheat_rng = random.Random(f"cheats-{seed}")  # noqa: S311
    claims = run_cheats(cfg, cheats_config(), noise_model(), rng, cheat_rng)
    return {"seed": seed, "claims": {name: asdict(c) for name, c in claims.items()}}


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
        ]
        out[name] = {g: Rate(sum(1 for c in claims if test(c)), n) for g, test in gates}
    return out


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
    return "\n".join(lines)


def main(argv: Sequence[str]) -> None:
    """Run every task in parallel, write the rows to ``argv[0]`` and print the summary."""
    cc = cheats_config()
    seeds = [_SEED_BASE + i for i in range(cc.runs)]
    with ProcessPoolExecutor(cc.workers) as pool:
        rows = list(pool.map(run_task, seeds, chunksize=4))
    with open(argv[0], "w", encoding="utf-8") as fh:  # noqa: PTH123 - a path given by the caller
        json.dump(rows, fh)
    print(summarize(rows, cc))  # noqa: T201 - this is a command-line tool


if __name__ == "__main__":
    main(sys.argv[1:])
