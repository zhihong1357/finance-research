# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""The data profile of research universe U1 (Phase 2.8), on the exploration segment only.

U1's first use for research, so it reads through the point-in-time door with
the clock at the end of the exploration segment (decision 0015): nothing from
the confirmation segment or the vault can reach it.

Three things, month by month:

* **How many contracts.** Contracts with any 1-minute bar, and contracts whose
  median daily turnover (quote volume) is at least each of ``liquid_usd``.
* **How liquidity is spread.** Quantiles of the contracts' median daily turnover.
* **Effective breadth.** At the end of each quarter, the daily returns of the
  contracts liquid over the last ``window_days`` with no missing day, and their
  correlation matrix ``C``. With ``N`` contracts, the participation ratio
  ``N_eff = (sum of eigenvalues)^2 / (sum of squared eigenvalues)
  = N^2 / sum of C_ij^2`` counts how many independent bets they are worth
  (``N`` if uncorrelated, 1 if they all move together). Crypto moves with
  bitcoin, so the same count after taking out the first principal component
  (the market) is given too: ``(N - l1)^2 / (sum C_ij^2 - l1^2)``, with ``l1``
  the largest eigenvalue, found by power iteration.

Returns are close-to-close over UTC days from the last 1-minute close of each
day. The results are derived from third-party data (decision 0008): they go to
the private dataset; the log says only what was done.
"""

import itertools
import json
import math
import tempfile
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import month_start
from finresearch.data import bars
from finresearch.data.fixed import Column
from finresearch.data.partitioned import PartitionedTable
from finresearch.data.pit import Clock, PointInTime, end_of_day
from finresearch.data.store import Limits, Remote, push
from finresearch.data.table import EVENT_TIME
from finresearch.registry.models import Boundaries

SYMBOL = "symbol"
_POWER_STEPS = 200
_POWER_TOLERANCE = 1e-10
_ZERO = 1e-12  # below this, what is left after the market is nothing


class ProfileError(RuntimeError):
    """Nothing to profile."""


@dataclass(frozen=True)
class Day:
    """One contract's UTC day: the last close and the day's turnover."""

    close: float
    turnover: float


Days = dict[str, dict[date, Day]]  # symbol -> day -> values


def daily(table: pa.Table) -> Days:
    """Collapse 1-minute bars (one exchange) to days: last close, summed quote volume."""
    if table.num_rows == 0:
        return {}
    close = _number(table, "close")
    turnover = _number(table, "quote_volume")
    days = pc.floor_temporal(table.column(EVENT_TIME), unit="day")
    t = pa.table(
        {
            SYMBOL: table.column(SYMBOL),
            "day": days,
            EVENT_TIME: table.column(EVENT_TIME),
            "c": close,
            "q": turnover,
        }
    )
    t = t.sort_by([(SYMBOL, "ascending"), (EVENT_TIME, "ascending")])
    grouped = t.group_by([SYMBOL, "day"], use_threads=False).aggregate(
        [("c", "last"), ("q", "sum")]
    )
    out: Days = defaultdict(dict)
    for row in grouped.to_pylist():
        out[row[SYMBOL]][row["day"].date()] = Day(float(row["c_last"]), float(row["q_sum"]))
    return dict(out)


def _number(table: pa.Table, name: str) -> Column:
    m = table.column(name).combine_chunks().cast(pa.float64())
    s = table.column(bars.scale_column(name)).combine_chunks().cast(pa.float64())
    return pc.multiply(m, pc.power(pa.scalar(10.0), pc.negate(s)))


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def quantile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between order statistics (the usual 'type 7')."""
    s = sorted(values)
    if not s:
        return math.nan
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


@dataclass(frozen=True)
class Month:
    month: str
    contracts: int
    liquid: dict[str, int]  # threshold in USD -> contracts at or above it
    turnover_quantiles: dict[str, float]  # quantile -> median daily turnover in USD


def month_profile(
    month: date, days: Days, liquid_usd: Sequence[float], quantiles: Sequence[float]
) -> Month:
    medians = [_median([d.turnover for d in v.values()]) for v in days.values() if v]
    return Month(
        f"{month:%Y-%m}",
        len(medians),
        {f"{x:g}": sum(m >= x for m in medians) for x in liquid_usd},
        {f"{q:g}": quantile(medians, q) for q in quantiles},
    )


@dataclass(frozen=True)
class Breadth:
    at: str  # the window's last day
    contracts: int
    effective: float  # participation ratio of the correlation matrix
    effective_ex_market: float  # the same without the first principal component
    market_share: float  # first eigenvalue / N: the share of variance the market explains


def correlation(returns: Sequence[Sequence[float]]) -> list[list[float]]:
    """Pearson correlation of each pair of series (all the same length)."""
    z: list[list[float]] = []
    for r in returns:
        mean = sum(r) / len(r)
        dev = [x - mean for x in r]
        norm = math.sqrt(sum(d * d for d in dev))
        z.append([d / norm for d in dev] if norm > 0 else [0.0] * len(r))
    return [[sum(a * b for a, b in zip(x, y, strict=True)) for y in z] for x in z]


def largest_eigenvalue(c: Sequence[Sequence[float]]) -> float:
    """Power iteration on a symmetric positive semi-definite matrix."""
    n = len(c)
    v = [1.0 / math.sqrt(n)] * n
    value = 0.0
    for _ in range(_POWER_STEPS):
        w = [sum(row[j] * v[j] for j in range(n)) for row in c]
        norm = math.sqrt(sum(x * x for x in w))
        if norm == 0:
            return 0.0
        v = [x / norm for x in w]
        if abs(norm - value) <= _POWER_TOLERANCE * max(norm, 1.0):
            return norm
        value = norm
    return value


def breadth(at: date, returns: Mapping[str, Sequence[float]]) -> Breadth:
    """Effective breadth of the given return series (one per contract, aligned)."""
    names = sorted(returns)
    n = len(names)
    if n < 2:  # noqa: PLR2004 - a correlation needs two series
        return Breadth(f"{at}", n, float(n), float(n), 1.0)
    c = correlation([returns[s] for s in names])
    squares = sum(x * x for row in c for x in row)
    l1 = largest_eigenvalue(c)
    rest = squares - l1 * l1
    ex_market = (n - l1) ** 2 / rest if rest > _ZERO else 0.0
    return Breadth(f"{at}", n, n * n / squares, ex_market, l1 / n)


def window_returns(
    days: Days, end: date, window_days: int, min_turnover: float
) -> dict[str, list[float]]:
    """Daily log returns over ``window_days`` up to ``end``: liquid contracts with every day."""
    span = [end - timedelta(days=window_days - i) for i in range(window_days + 1)]  # one more close
    out: dict[str, list[float]] = {}
    for symbol, by_day in days.items():
        if not all(d in by_day for d in span):
            continue
        if _median([by_day[d].turnover for d in span[1:]]) < min_turnover:
            continue
        closes = [by_day[d].close for d in span]
        if min(closes) <= 0:
            continue
        out[symbol] = [math.log(b / a) for a, b in itertools.pairwise(closes)]
    return out


def quarter_ends(start: date, end: date) -> list[date]:
    """The last days of the quarters that end inside ``[start, end]``."""
    out: list[date] = []
    m = month_start(start)
    while m <= end:
        if m.month in (3, 6, 9, 12):
            last = month_start(m, 1) - timedelta(days=1)
            if start <= last <= end:
                out.append(last)
        m = month_start(m, 1)
    return out


@dataclass
class Profile:
    universe: str
    exchange: str
    exploration: str
    months: list[Month] = field(default_factory=lambda: list[Month]())
    breadth: list[Breadth] = field(default_factory=lambda: list[Breadth]())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1, sort_keys=True) + "\n"


Reader = Callable[[date], pa.Table]  # one month of one exchange's bars, through the door


def profile(boundaries: Boundaries, exchange: str, read: Reader, cfg: Mapping[str, Any]) -> Profile:
    """The whole profile, reading one month at a time."""
    seg = boundaries.exploration
    out = Profile(boundaries.universe, exchange, f"{seg.start}..{seg.end}")
    liquid = [float(x) for x in cfg["liquid_usd"]]
    qs = [float(q) for q in cfg["quantiles"]]
    all_days: Days = defaultdict(dict)
    m = month_start(seg.start)
    while m <= seg.end:
        days = daily(read(m))
        out.months.append(month_profile(m, days, liquid, qs))
        for symbol, by_day in days.items():
            all_days[symbol].update(by_day)
        m = month_start(m, 1)
    if not any(mo.contracts for mo in out.months):
        raise ProfileError(f"no {exchange} bars in the exploration segment")
    window, floor = int(cfg["window_days"]), float(cfg["breadth_min_turnover_usd"])
    for end in quarter_ends(seg.start + timedelta(days=window), seg.end):
        out.breadth.append(breadth(end, window_returns(all_days, end, window, floor)))
    return out


def door_reader(
    boundaries: Boundaries, remote: Remote, limits: Limits, fmt: int, exchange: str
) -> Reader:
    """Months of bars through the point-in-time door, clock at the end of exploration.

    Each month gets its own scratch directory, emptied after the read: a month
    of bars is hundreds of MB and fifty of them do not fit on a runner.
    """
    clock = Clock(
        limit=end_of_day(boundaries.exploration.end), vault_start=_start(boundaries.vault.start)
    )
    columns = [
        "close",
        bars.scale_column("close"),
        "quote_volume",
        bars.scale_column("quote_volume"),
    ]

    def read(month: date) -> pa.Table:
        with tempfile.TemporaryDirectory(prefix="finresearch-profile-") as tmp:
            table = PartitionedTable(bars.NAME, bars.KEYS, fmt, Path(tmp), remote, limits)
            door = PointInTime(clock, [table])
            last = end_of_day(month_start(month, 1) - timedelta(days=1))  # past the clock: refused
            snap = door.window(
                bars.NAME, _start(month), last, where={"exchange": {exchange}}, columns=columns
            )
            return snap.table

    return read


def _start(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=UTC)


def save(p: Profile, remote: Remote, limits: Limits, work: Path, today: date) -> str:
    """Write the profile to the private dataset; returns its path there."""
    path = f"profile/{p.universe.lower()}/{p.exchange}/{today:%Y-%m-%d}.json"
    local = work / path
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(p.to_json(), encoding="utf-8")
    push(work, remote, path.rsplit("/", 1)[0], limits, f"profile: {p.universe} {p.exchange}")
    return path
