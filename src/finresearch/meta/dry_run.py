"""The bench's small-scale dry run (decision 0024): ``python -m finresearch.meta.dry_run OUT.json``.

Four settings on noise (the momentum/reversal family and the many-hypotheses
mode, each without and with costs) and a detection grid (planted strength x
data length, with costs), sizes from ``config/meta.toml``'s ``[dry_run]``.
Every task has its own seed, so the rows are the same on every machine and in
any order of execution; ``summarize`` turns them into the rates that the
dry-run report quotes. Not for setting thresholds: a few hundred runs cannot
resolve a 0.135% rate (decision 0023 asks for 40,062).
"""

import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from statistics import fmean, stdev
from typing import Any

from finresearch.core.config import load_toml
from finresearch.meta.bench import bench_config, run_family, run_many
from finresearch.meta.noise import noise_model
from finresearch.stats.montecarlo import wilson_interval
from finresearch.verdict import Thresholds

_SEED_BLOCK = 1_000_000  # task i of setting k has seed k * _SEED_BLOCK + i
_CHECK = 0.99


@dataclass(frozen=True)
class Task:
    mode: str  # "family" or "many"
    costs: bool
    strength: float
    years: int  # 0: the configured world length
    seed: int


def tasks(config: Mapping[str, Any] | None = None) -> list[Task]:
    """Every task of the dry run, in a fixed order with fixed seeds."""
    cfg = (load_toml("meta") if config is None else config)["dry_run"]
    out: list[Task] = []
    settings = (("family", False), ("family", True), ("many", False), ("many", True))
    for k, (mode, costs) in enumerate(settings, start=1):
        out += [Task(mode, costs, 0.0, 0, k * _SEED_BLOCK + i) for i in range(int(cfg["runs"]))]
    k = 10
    for strength in cfg["strengths"]:
        for years in cfg["years"]:
            k += 1
            out += [
                Task("many", True, float(strength), int(years), k * _SEED_BLOCK + i)
                for i in range(int(cfg["grid_runs"]))
            ]
    return out


def run_task(task: Task) -> dict[str, Any]:
    """One simulated hypothesis, as a flat record."""
    meta = load_toml("meta")
    over: dict[str, object] = {} if task.costs else {"cost": 0.0}
    if task.years:
        over["days"] = task.years * int(meta["dry_run"]["days_per_year"])
    cfg = bench_config(meta, **over)
    rng = random.Random(task.seed)  # noqa: S311 - a simulation's seeded stream, not a secret
    model = noise_model()
    if task.mode == "family":
        r = run_family(cfg, model, rng)
    else:
        r = run_many(cfg, model, rng, task.strength)
    return {
        "mode": task.mode,
        "cost": task.costs,
        "strength": task.strength,
        "years": task.years,
        "seed": task.seed,
        "selected": r.selected,
        "selected_index": r.selected_index,
        "exploration_sharpe": r.exploration_sharpe,
        "k_eff": r.effective_trials,
        "t_nw": r.t_newey_west,
        "t_no": r.t_nonoverlapping,
        "dsr": r.dsr,
        "pbo": r.pbo,
        "pass_nw": r.verdict_newey_west.passed,
        "pass_no": r.verdict_nonoverlapping.passed,
    }


def _rate(hits: int, n: int) -> str:
    low, high = wilson_interval(hits, n, _CHECK)
    return f"{hits}/{n} = {100 * hits / n:.1f}% (99%: {100 * low:.1f}% to {100 * high:.1f}%)"


def summarize(rows: Sequence[Mapping[str, Any]]) -> str:
    """The rates the report quotes, as plain text."""
    lines: list[str] = []
    th = Thresholds.defaults()
    for mode in ("family", "many"):
        for cost in (False, True):
            g = [r for r in rows if r["mode"] == mode and r["cost"] == cost and not r["years"]]
            if not g:
                continue
            n = len(g)
            lines.append(f"{mode}, costs {'on' if cost else 'off'}: {n} runs")
            gates: list[tuple[str, Callable[[Mapping[str, Any]], bool]]] = [
                (f"t >= {th.t:g} (Newey-West)", lambda r: r["t_nw"] >= th.t),
                (f"t >= {th.t:g} (non-overlapping)", lambda r: r["t_no"] >= th.t),
                (f"deflated Sharpe >= {th.dsr_min:g}", lambda r: r["dsr"] >= th.dsr_min),
                (f"PBO <= {th.pbo_max:g}", lambda r: r["pbo"] <= th.pbo_max),
                ("pipeline (Newey-West)", lambda r: bool(r["pass_nw"])),
                ("pipeline (non-overlapping)", lambda r: bool(r["pass_no"])),
            ]
            for name, test in gates:
                lines.append(f"  {name}: {_rate(sum(1 for r in g if test(r)), n)}")
            for key in ("t_nw", "t_no"):
                values = [r[key] for r in g]
                lines.append(f"  {key}: mean {fmean(values):.3f}, sd {stdev(values):.3f}")
            keff = sorted(Counter(r["k_eff"] for r in g).items())
            lines.append(f"  effective trials: {dict(keff)}")
            lines.append(f"  mean PBO {fmean(r['pbo'] for r in g):.3f}")
    cells: dict[tuple[float, int], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["years"]:
            cells[(r["strength"], r["years"])].append(r)
    for (strength, years), g in sorted(cells.items()):
        found = sum(1 for r in g if r["selected_index"] == 0)
        supported = sum(1 for r in g if r["selected_index"] == 0 and r["pass_no"])
        lines.append(
            f"detection: strength {strength}, {years} years: kept {found}/{len(g)}, "
            f"supported {supported}/{len(g)}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str]) -> None:
    """Run every task in parallel, write the rows to ``argv[0]`` and print the summary."""
    workers = int(load_toml("meta")["dry_run"]["workers"])
    with ProcessPoolExecutor(workers) as pool:
        rows = list(pool.map(run_task, tasks(), chunksize=4))
    with open(argv[0], "w", encoding="utf-8") as fh:  # noqa: PTH123 - a path given by the caller
        json.dump(rows, fh)
    print(summarize(rows))  # noqa: T201 - this is a command-line tool


if __name__ == "__main__":
    main(sys.argv[1:])
