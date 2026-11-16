# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Quality checks of research universe U1 (Phase 2.7).

The tables already refuse what is malformed (``PitTable``, the builders) and
note bars that are impossible (``bars.problems``). These checks look for what
is well formed but suspicious, month by month, and count it:

* **format**: a partition's columns and types differ from the table's;
* **range**: prices that are not positive, bars that are impossible, a
  1-minute move larger than ``max_move``, a funding rate beyond
  ``max_abs_rate`` or with an interval that is not a whole divisor of a day,
  a settlement off the hour, negative open interest;
* **gaps**: minutes (or 5-minute readings) missing between a contract's first
  and last row of the month;
* **freshness**: the newest stored month older than the archive allows;
* **across sources**: the same contract's 1-minute close at Binance and at
  Bybit, and Binance's last price against its own mark price, further apart
  than ``max_gap``.

Every check is a ``Finding``: how many rows it found out of how many it looked
at, and whether that share is above the check's ``max_share``. A finding can
be real (a crash moves a price 30% in a minute); the checks flag, they do not
change the data. The thresholds are in ``config/quality.toml``.

A month's report names contracts and times, which are derived from
third-party data, so it is written to the private dataset only; the public log
carries counts (decision 0008).
"""

import itertools
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc, month_start
from finresearch.data import bars, funding, mark, open_interest
from finresearch.data.fixed import Column, Fixed
from finresearch.data.partitioned import month_parts, read_part
from finresearch.data.store import Limits, Remote, push
from finresearch.data.table import EVENT_TIME, PitTable

_US = 1_000_000
_HOUR_US = 3600 * _US
_DAY_HOURS = 24
SYMBOL = "symbol"


class QualityError(RuntimeError):
    """A table or month that cannot be checked."""


@dataclass(frozen=True)
class Finding:
    check: str
    count: int  # rows found
    total: int  # rows looked at
    max_share: float
    example: str = ""  # the first row found: contract and time (private report only)
    top: tuple[tuple[str, int], ...] = ()  # the contracts found most often (private report only)

    @property
    def flagged(self) -> bool:
        return self.count > self.max_share * self.total

    def line(self) -> str:
        """For the public log: counts only."""
        return f"{self.check}: {self.count} of {self.total}{' FLAGGED' if self.flagged else ''}"


# --- the expected layout of each table ---------------------------------------------------------


def _empty_fixed() -> Fixed:
    return Fixed(pa.array([], pa.int64()), 0)


def expected_schema(table: str) -> pa.Schema:
    """The columns and types a partition of ``table`` must have: an empty table from its builder."""
    none: Column = pa.array([], pa.int64())
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    if table == bars.NAME:
        t = bars.bars("x", "x", none, {n: _empty_fixed() for n in bars.DECIMALS}, none, epoch)
    elif table == funding.NAME:
        t = funding.funding("x", "x", none, _empty_fixed(), none, epoch)
    elif table == mark.NAME:
        t = mark.marks("x", "x", none, _empty_fixed(), none, epoch)
    elif table == open_interest.NAME:
        values = {n: _empty_fixed() for n in open_interest.DECIMALS}
        t = open_interest.readings("x", "x", none, 0, values, epoch)
    else:
        raise QualityError(f"no quality checks for {table}")
    return t.table.schema.remove_metadata()


def schema_finding(found: pa.Schema, table: str) -> Finding:
    # Stored partitions keep their columns in name order: compare names and types, not order.
    expected = {(f.name, str(f.type)) for f in expected_schema(table)}
    differs = {(f.name, str(f.type)) for f in found} != expected
    detail = "" if not differs else f"columns {found.names}, types {[str(t) for t in found.types]}"
    return Finding("format: columns or types differ from the table's", int(differs), 1, 0.0, detail)


# --- helpers ------------------------------------------------------------------------------------


def _number(table: pa.Table, name: str, scale: str) -> Column:
    """A fixed-point column as floats, for comparisons that need no exact arithmetic."""
    m = table.column(name).combine_chunks().cast(pa.float64())
    s = table.column(scale).combine_chunks().cast(pa.float64())
    return pc.multiply(m, pc.power(pa.scalar(10.0), pc.negate(s)))


def _sorted(table: pa.Table) -> pa.Table:
    return table.sort_by([(SYMBOL, "ascending"), (EVENT_TIME, "ascending")]).combine_chunks()


def _count(mask: Column) -> int:
    return int(pc.sum(pc.cast(pc.fill_null(mask, pa.scalar(False)), pa.int64())).as_py() or 0)


def _example(table: pa.Table, mask: Column) -> str:
    rows = table.filter(pc.fill_null(mask, pa.scalar(False))).slice(0, 1).to_pylist()
    return f"{rows[0][SYMBOL]} {ensure_utc(rows[0][EVENT_TIME]):%Y-%m-%d %H:%M}" if rows else ""


def _finding(check: str, table: pa.Table, mask: Column, total: int, max_share: float) -> Finding:
    return Finding(check, _count(mask), total, max_share, _example(table, mask))


def _mod(a: Column, b: Column) -> Column:
    """``a mod b`` for non-negative integers (integer division truncates)."""
    return pc.subtract(a, pc.multiply(pc.divide(a, b), b))


def _consecutive(table: pa.Table) -> tuple[pa.Table, Column]:
    """The later row of each pair of consecutive rows, and where the pair is one contract."""
    names = table.column(SYMBOL)
    if table.num_rows < 2:  # noqa: PLR2004 - a pair needs two rows
        return table.slice(0, 0), pa.array([], pa.bool_())
    return table.slice(1), pc.equal(names[1:], names[:-1])


def moves(table: pa.Table, price: Column, max_move: float, max_share: float, what: str) -> Finding:
    """Consecutive rows of one contract whose price changes by more than ``max_move``."""
    later, same = _consecutive(table)
    if later.num_rows == 0:
        return Finding(
            f"range: {what} moved more than {max_move:.0%} from the row before", 0, 0, max_share
        )
    change = pc.abs(pc.subtract(pc.divide(price[1:], price[:-1]), pa.scalar(1.0)))
    mask = pc.and_(same, pc.greater(change, pa.scalar(max_move)))
    return _finding(
        f"range: {what} moved more than {max_move:.0%} from the row before",
        later,
        mask,
        _count(same),
        max_share,
    )


def gaps(table: pa.Table, step: timedelta, max_share: float) -> Finding:
    """Steps missing between consecutive rows of one contract, out of all the steps its span has."""
    check = f"gaps: {step.total_seconds() / 60:.0f}-minute steps missing inside a contract's month"
    later, same = _consecutive(table)
    if later.num_rows == 0:
        return Finding(check, 0, table.num_rows, max_share)
    times = table.column(EVENT_TIME).combine_chunks().cast(pa.int64())
    steps = pc.divide(
        pc.subtract(times[1:], times[:-1]), pa.scalar(int(step.total_seconds() * _US))
    )
    missing = pc.if_else(same, pc.subtract(steps, pa.scalar(1)), pa.scalar(0))
    holes = pc.greater(missing, pa.scalar(0))
    count = int(pc.sum(missing).as_py() or 0)
    return Finding(check, count, table.num_rows + count, max_share, _example(later, holes))


# --- per table ----------------------------------------------------------------------------------


def bar_findings(t: pa.Table, cfg: Mapping[str, Any]) -> list[Finding]:
    table = _sorted(t)
    n = table.num_rows
    prices = [_number(table, p, bars.scale_column(p)) for p in bars.PRICES]
    not_positive = pc.less_equal(pc.min_element_wise(*prices), pa.scalar(0.0))
    impossible = sum(
        int(line.split(": ", 1)[1].split(" ", 1)[0])
        for line in bars.problems(PitTable(bars.NAME, bars.KEYS, table))
    )
    return [
        _finding("range: a price is not positive", table, not_positive, n, 0.0),
        Finding(
            "range: impossible bars (bars.problems)",
            impossible,
            n,
            float(cfg["max_impossible_share"]),
        ),
        moves(table, prices[3], float(cfg["max_move"]), float(cfg["max_move_share"]), "the close"),
        gaps(table, bars.BAR, float(cfg["max_gap_share"])),
    ]


def mark_findings(t: pa.Table, cfg: Mapping[str, Any]) -> list[Finding]:
    table = _sorted(t)
    price = _number(table, mark.CLOSE, mark.CLOSE_SCALE)
    return [
        _finding(
            "range: a mark price is not positive",
            table,
            pc.less_equal(price, pa.scalar(0.0)),
            table.num_rows,
            0.0,
        ),
        moves(table, price, float(cfg["max_move"]), float(cfg["max_move_share"]), "the mark price"),
        gaps(table, timedelta(minutes=1), float(cfg["max_gap_share"])),
    ]


def funding_findings(t: pa.Table, cfg: Mapping[str, Any]) -> list[Finding]:
    table = _sorted(t)
    n = table.num_rows
    rate = _number(table, funding.RATE, funding.RATE_SCALE)
    cap = float(cfg["max_abs_rate"])
    hours = table.column(funding.INTERVAL).combine_chunks().cast(pa.int64())
    bad = pc.less_equal(hours, pa.scalar(0))
    safe = pc.if_else(bad, pa.scalar(1, pa.int64()), hours)
    day = pa.array([_DAY_HOURS] * n, pa.int64())
    odd: Column = pc.or_(bad, pc.not_equal(_mod(day, safe), pa.scalar(0)))
    since_hour = _mod(
        table.column(EVENT_TIME).combine_chunks().cast(pa.int64()),
        pa.array([_HOUR_US] * n, pa.int64()),
    )
    slack = int(float(cfg["settlement_slack_seconds"]) * _US)
    off = pc.and_(
        pc.greater(since_hour, pa.scalar(slack)), pc.less(since_hour, pa.scalar(_HOUR_US - slack))
    )
    return [
        _finding(
            f"range: funding rate beyond ±{cap:.1%}",
            table,
            pc.greater(pc.abs(rate), pa.scalar(cap)),
            n,
            0.0,
        ),
        _finding("range: funding interval not a whole divisor of 24 hours", table, odd, n, 0.0),
        _finding("range: settlement off the hour", table, off, n, float(cfg["max_off_hour_share"])),
    ]


def open_interest_findings(t: pa.Table, cfg: Mapping[str, Any]) -> list[Finding]:
    table = _sorted(t)
    n = table.num_rows
    negative = pa.array([False] * n)
    for q in open_interest.QUANTITIES:
        negative = pc.or_(negative, pc.less(table.column(q), pa.scalar(0, pa.int64())))
    return [
        _finding("range: negative open interest", table, negative, n, 0.0),
        gaps(table, timedelta(minutes=5), float(cfg["max_gap_share"])),
    ]


def _closes(table: pa.Table, name: str, scale: str) -> pa.Table:
    """(contract, minute, value), keeping only minutes with trades when the table counts them.

    A minute without a trade repeats the last price, which goes stale while the
    other source moves; comparing it says nothing about either source.
    """
    if bars.TRADES in table.column_names:
        table = table.filter(pc.greater(table.column(bars.TRADES), pa.scalar(0, pa.int64())))
    return pa.table(
        {
            SYMBOL: table.column(SYMBOL),
            EVENT_TIME: table.column(EVENT_TIME),
            "x": _number(table, name, scale),
        }
    )


def apart(left: pa.Table, right: pa.Table, max_gap: float, max_share: float, what: str) -> Finding:
    """Rows of one contract and minute in both tables further apart than ``max_gap``."""
    check = f"across sources: {what} more than {max_gap:.1%} apart"
    both = left.join(
        right.rename_columns([SYMBOL, EVENT_TIME, "y"]), [SYMBOL, EVENT_TIME], join_type="inner"
    )
    if both.num_rows == 0:
        return Finding(check, 0, 0, max_share)
    both = _sorted(both)
    gap = pc.abs(pc.subtract(pc.divide(both.column("x"), both.column("y")), pa.scalar(1.0)))
    mask = pc.greater(gap, pa.scalar(max_gap))
    f = _finding(check, both, mask, both.num_rows, max_share)
    return Finding(f.check, f.count, f.total, f.max_share, f.example, _top(both, mask))


_TOP = 5


def _top(table: pa.Table, mask: Column) -> tuple[tuple[str, int], ...]:
    """The contracts with the most rows found, and how many."""
    found = table.filter(pc.fill_null(mask, pa.scalar(False)))
    if found.num_rows == 0:
        return ()
    counts = found.group_by(SYMBOL).aggregate([([], "count_all")])
    rows = sorted(counts.to_pylist(), key=lambda r: (-r["count_all"], r[SYMBOL]))[:_TOP]
    return tuple((str(r[SYMBOL]), int(r["count_all"])) for r in rows)


def cross_exchange(binance: pa.Table, bybit: pa.Table, cfg: Mapping[str, Any]) -> Finding:
    close = bars.scale_column("close")
    return apart(
        _closes(binance, "close", close),
        _closes(bybit, "close", close),
        float(cfg["max_gap"]),
        float(cfg["max_gap_share"]),
        "Binance and Bybit 1-minute closes",
    )


def last_vs_mark(binance: pa.Table, marks: pa.Table, cfg: Mapping[str, Any]) -> Finding:
    return apart(
        _closes(binance, "close", bars.scale_column("close")),
        _closes(marks, mark.CLOSE, mark.CLOSE_SCALE),
        float(cfg["max_gap"]),
        float(cfg["max_gap_share"]),
        "Binance last price and mark price",
    )


def freshness(stored: Sequence[date], today: date, settle_days: int) -> Finding:
    """Whether the newest stored month is the newest the archive allows ``settle_days`` after it."""
    expected = month_start(month_start(today - timedelta(days=settle_days)), -1)
    newest = max(stored) if stored else None
    stale = newest is None or newest < expected
    shown = (
        f"newest {newest:%Y-%m}, expected {expected:%Y-%m}"
        if newest
        else f"none, expected {expected:%Y-%m}"
    )
    return Finding(
        "freshness: newest month older than the archive allows", int(stale), 1, 0.0, shown
    )


# --- running on the store -----------------------------------------------------------------------

CHECKERS = {
    bars.NAME: bar_findings,
    mark.NAME: mark_findings,
    funding.NAME: funding_findings,
    open_interest.NAME: open_interest_findings,
}


@dataclass(frozen=True)
class Report:
    table: str
    month: str  # YYYY-MM, or "" for the table-wide freshness check
    findings: list[Finding]

    @property
    def flagged(self) -> list[Finding]:
        return [f for f in self.findings if f.flagged]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def check_month(
    table: str,
    month: date,
    parts: Mapping[str, pa.Table],
    cfg: Mapping[str, Any],
    marks: Mapping[str, pa.Table] | None = None,
) -> Report:
    """All checks of one table and month. ``marks`` (for the bars) adds last price against mark."""
    findings: list[Finding] = []
    for part, rows in sorted(parts.items()):
        findings += part_findings(table, part, rows, cfg)
    closes = (
        {p: _closes(r, "close", bars.scale_column("close")) for p, r in parts.items()}
        if table == bars.NAME
        else {}
    )
    mark_closes = (
        _closes(marks["binance"], mark.CLOSE, mark.CLOSE_SCALE)
        if marks and "binance" in marks
        else None
    )
    findings += across(closes, mark_closes, cfg)
    return Report(table, f"{month:%Y-%m}", findings)


def part_findings(table: str, part: str, rows: pa.Table, cfg: Mapping[str, Any]) -> list[Finding]:
    rules = {k: v for k, v in dict(cfg["tables"][table]).items() if k != "parts"}
    rules |= dict(cfg["tables"][table].get("parts", {}).get(part, {}))  # a part's own limits
    batch = int(cfg["contracts_per_batch"])
    found = [schema_finding(rows.schema, table), *batched(CHECKERS[table], rows, rules, batch)]
    return [Finding(f"{part}: {f.check}", f.count, f.total, f.max_share, f.example) for f in found]


Checker = Callable[[pa.Table, Mapping[str, Any]], list[Finding]]


def batched(
    checker: Checker, rows: pa.Table, rules: Mapping[str, Any], contracts: int
) -> list[Finding]:
    """Run ``checker`` on batches of ``contracts`` contracts and add the counts up.

    Every check looks at one row or at one contract's consecutive rows, so
    batches cut at contract boundaries find what the whole table would; the
    copies a check makes are then a batch, not a month of tens of millions of rows.
    """
    merged: dict[str, Finding] = {}
    order: list[str] = []
    for part in _contract_batches(rows, contracts):
        for f in checker(part, rules):
            seen = merged.get(f.check)
            if seen is None:
                merged[f.check] = f
                order.append(f.check)
            else:
                example = seen.example or f.example
                merged[f.check] = Finding(
                    f.check, seen.count + f.count, seen.total + f.total, f.max_share, example
                )
    return [merged[c] for c in order]


def _contract_batches(rows: pa.Table, contracts: int) -> list[pa.Table]:
    """Slices of ``rows`` of whole contracts, at most ``contracts`` each (sorted by contract)."""
    if rows.num_rows == 0:
        return [rows]
    names = rows.column(SYMBOL).combine_chunks()
    if not pc.all(pc.greater_equal(names[1:], names[:-1])).as_py():
        rows = rows.sort_by([(SYMBOL, "ascending"), (EVENT_TIME, "ascending")])
        names = rows.column(SYMBOL).combine_chunks()
    changes = pc.indices_nonzero(pc.not_equal(names[1:], names[:-1])).to_pylist()
    starts = [0, *[int(i) + 1 for i in changes if i is not None]]
    cuts = [*starts[::contracts], rows.num_rows]
    return [rows.slice(a, b - a) for a, b in itertools.pairwise(cuts)]


def across(
    closes: Mapping[str, pa.Table], mark_closes: pa.Table | None, cfg: Mapping[str, Any]
) -> list[Finding]:
    """The checks across sources, on closes only (``_closes``): exchanges, and last against mark."""
    out: list[Finding] = []
    if {"binance", "bybit"} <= set(closes):
        c = cfg["cross_exchange"]
        what = "Binance and Bybit 1-minute closes"
        out.append(
            apart(
                closes["binance"],
                closes["bybit"],
                float(c["max_gap"]),
                float(c["max_gap_share"]),
                what,
            )
        )
    if mark_closes is not None and "binance" in closes:
        c = cfg["last_vs_mark"]
        what = "Binance last price and mark price"
        out.append(
            apart(
                closes["binance"], mark_closes, float(c["max_gap"]), float(c["max_gap_share"]), what
            )
        )
    return out


def stream_month(
    table: str,
    month: date,
    files: Mapping[str, Path],
    cfg: Mapping[str, Any],
    mark_file: Path | None,
) -> Report:
    """``check_month`` reading one part at a time, keeping only closes between parts.

    A month of 1-minute bars is tens of millions of rows per exchange; holding
    both exchanges and the mark prices at once ran a runner out of memory.
    """
    findings: list[Finding] = []
    closes: dict[str, pa.Table] = {}
    for part, path in sorted(files.items()):
        rows = read_part(path)
        findings += part_findings(table, part, rows, cfg)
        if table == bars.NAME:
            closes[part] = _closes(rows, "close", bars.scale_column("close"))
        del rows
    mark_closes = None
    if table == bars.NAME and mark_file is not None:
        cols = [SYMBOL, EVENT_TIME, mark.CLOSE, mark.CLOSE_SCALE]
        mark_closes = _closes(read_part(mark_file, cols), mark.CLOSE, mark.CLOSE_SCALE)
    findings += across(closes, mark_closes, cfg)
    return Report(table, f"{month:%Y-%m}", findings)


def report_path(table: str, fmt: int, run_day: date, month: str) -> str:
    """Where a report goes in the private dataset (it names contracts and times).

    Each run has its own folder: uploads never replace a file already stored.
    """
    return f"quality/{table}/v{fmt}/{run_day:%Y-%m-%d}/{month or 'table'}/report.json"


def stored_months(table: str, fmt: int, remote: Remote) -> list[date]:
    names = {p.split("/")[-2] for p in remote.list(f"canonical/{table}/v{fmt}")}
    return sorted(date(int(n[:4]), int(n[5:7]), 1) for n in names if len(n) == 7 and n[4] == "-")  # noqa: PLR2004 - YYYY-MM


@dataclass(frozen=True)
class Run:
    table: str
    months: int
    findings: int
    flagged: dict[str, int]  # check -> months in which it was flagged
    fresh: Finding


def run(
    table: str,
    remote: Remote,
    limits: Limits,
    work: Path,
    today: date,
    cfg: Mapping[str, Any],
    u1: Mapping[str, Any],
    only: date | None = None,
    progress: Callable[[str], None] = lambda _line: None,
) -> Run:
    """Check every stored month of ``table`` (or ``only``); write the reports privately."""
    if table not in CHECKERS:
        raise QualityError(f"no quality checks for {table}")
    fmt = int(u1["tables"][table]["format"])
    months = stored_months(table, fmt, remote)
    settle = max(int(v["settle_days"]) for v in u1["tables"][table].values() if isinstance(v, dict))
    fresh = freshness(months, today, settle)
    reports = [Report(table, "", [fresh])]
    if only is not None:
        if only not in months:
            raise QualityError(f"{table} has no partition for {only:%Y-%m}")
        months = [only]
    mark_fmt = int(u1["tables"][mark.NAME]["format"])
    flagged: dict[str, int] = {}
    count = 0
    mark_months = set(stored_months(mark.NAME, mark_fmt, remote)) if table == bars.NAME else set()
    out = work / "reports"
    every = int(cfg["push_every_months"])
    for i, m in enumerate(months, start=1):
        data = work / "data"
        files = month_parts(table, fmt, m, remote, limits, data)
        mark_file = None
        if m in mark_months:
            mark_file = month_parts(mark.NAME, mark_fmt, m, remote, limits, data).get("binance")
        report = stream_month(table, m, files, cfg, mark_file)
        reports.append(report)
        count += len(report.findings)
        for f in report.flagged:
            flagged[f.check] = flagged.get(f.check, 0) + 1
        shutil.rmtree(data)  # a month of bars is hundreds of MB; the runner's disk is not
        progress(
            f"{table} {m:%Y-%m}: {len(report.findings)} check(s), {len(report.flagged)} flagged"
        )
        if i % every == 0 or i == len(months):  # a run cut short keeps what it checked
            _push(
                reports,
                out,
                fmt,
                today,
                remote,
                limits,
                f"quality: {table}, {i} of {len(months)} month(s)",
            )
            reports = []
    if reports:
        _push(reports, out, fmt, today, remote, limits, f"quality: {table}")
    return Run(table, len(months), count, flagged, fresh)


def _push(
    reports: Sequence[Report],
    out: Path,
    fmt: int,
    day: date,
    remote: Remote,
    limits: Limits,
    message: str,
) -> None:
    if out.exists():
        shutil.rmtree(out)
    for r in reports:
        local = out / report_path(r.table, fmt, day, r.month)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(r.to_json(), encoding="utf-8")
    table = reports[0].table
    push(out, remote, f"quality/{table}/v{fmt}", limits, message)
