# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Revised series: one table shape for macro figures, rates and positions (Phase 2.6.2).

A macro figure is published, then revised: first estimates of output, prices
and jobs change for months or years. A study at instant ``t`` may only see the
version that was public at ``t`` (docs/architecture.md 2.1), so every version
is kept. A row is one value of one observation in one version:

* key ``series``: the source's name for the series, for example ``ROUTPUT``;
* ``frequency``: ``D`` (a day), ``M`` (a calendar month) or ``Q`` (a calendar
  quarter); one per series;
* ``event_time``: the start of the observation period, midnight UTC of its first
  day (a day's position report, a month's CPI, a quarter's output);
* ``available_at``: when this version became public. A value cannot be known
  before its period is over, so ``available_at`` is at least the period's end;
* ``ingested_at``: when we fetched it;
* ``value``: an exact fixed-point mantissa (``data/fixed.py``) with its scale
  in ``value_scale``.

An observation missing from a version has no row in it; queries then see the
version before.

The door (``data/pit.py``) answers with ``versions_as_of``: for each observation
the latest version public at the query instant. ``compact`` drops versions that
repeat the value before them: a source like the Philadelphia Fed's real-time
data set republishes the whole history every month, and almost every value in
a new vintage equals the last one. What a query returns is the same, except
that ``available_at`` is the first time the value was public.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc, month_start, utc
from finresearch.data.fixed import MANTISSA_TYPE, SCALE_TYPE, Column, Fixed
from finresearch.data.table import AVAILABLE_AT, EVENT_TIME, INGESTED_AT, TIME_TYPE, PitTable

KEYS = ("series",)
SERIES = "series"
FREQUENCY = "frequency"
VALUE = "value"
VALUE_SCALE = "value_scale"
_MONTHS_PER_QUARTER = 3
_TEN = pa.scalar(10, MANTISSA_TYPE)


class Frequency(StrEnum):
    DAY = "D"
    MONTH = "M"
    QUARTER = "Q"


class VintageError(ValueError):
    """Input that cannot form a table of revised series."""


def period_end(start: date, frequency: Frequency) -> date:
    """The first day after the period that starts on ``start``."""
    if frequency is Frequency.DAY:
        return start + timedelta(days=1)
    if start.day != 1:
        raise VintageError(f"a {frequency.name.lower()} must start on the 1st, not {start}")
    if frequency is Frequency.MONTH:
        return month_start(start, 1)
    if (start.month - 1) % _MONTHS_PER_QUARTER:
        raise VintageError(f"a quarter starts in January, April, July or October, not {start}")
    return month_start(start, _MONTHS_PER_QUARTER)


def versions(
    name: str,
    series: str,
    frequency: Frequency,
    starts: Sequence[date],
    values: Fixed,
    available_at: Sequence[datetime],
    ingested_at: datetime,
) -> PitTable:
    """Versions of one series.

    Row ``i`` is the value of the period starting on ``starts[i]`` as public at ``available_at[i]``.
    """
    n = len(starts)
    if not (len(values) == len(available_at) == n):
        raise VintageError(f"{series}: columns of different lengths")
    if values.mantissa.null_count:
        raise VintageError(f"{series}: a value is missing (leave the row out instead)")
    known = [ensure_utc(t) for t in available_at]
    for start, t in zip(starts, known, strict=True):
        end = period_end(start, frequency)
        if t < utc(end.year, end.month, end.day):
            raise VintageError(
                f"{series}: the period from {start} is public at {t.isoformat()}, before it ends"
            )
    return PitTable(
        name,
        KEYS,
        pa.table(
            {
                SERIES: pa.array([series] * n, type=pa.string()),
                FREQUENCY: pa.array([frequency.value] * n, type=pa.string()),
                EVENT_TIME: pa.array([utc(d.year, d.month, d.day) for d in starts], TIME_TYPE),
                AVAILABLE_AT: pa.array(known, type=TIME_TYPE),
                INGESTED_AT: pa.array([ensure_utc(ingested_at)] * n, type=TIME_TYPE),
                VALUE: values.mantissa.cast(MANTISSA_TYPE),
                VALUE_SCALE: pa.array([values.scale] * n, type=SCALE_TYPE),
            }
        ),
    )


def concat(name: str, tables: Sequence[PitTable]) -> PitTable:
    """Tables of revised series as one table named ``name``; each series keeps one frequency."""
    for t in tables:
        if t.name != name:
            raise VintageError(f"cannot put {t.name!r} into {name!r}")
    table = pa.concat_tables([t.table for t in tables]) if tables else _empty()
    if table.num_rows:
        pairs = table.group_by([SERIES, FREQUENCY]).aggregate([])
        counts = pairs.group_by([SERIES]).aggregate([([], "count_all")])
        if pc.max(counts.column("count_all")).as_py() > 1:
            raise VintageError(f"{name}: a series has more than one frequency")
    return PitTable(name, KEYS, table)


class Conflicts(StrEnum):
    """What ``merge`` does when fetches disagree on the value of one version."""

    REFUSE = "refuse"  # a source that never changes what it published (an error if it does)
    LATEST = "latest"  # a curated data set that corrects its records: the latest fetch wins


def merge(
    name: str, tables: Sequence[PitTable], conflicts: Conflicts = Conflicts.REFUSE
) -> PitTable:
    """Tables from repeated fetches of one source as one table.

    The same version seen in several fetches is kept once, with the earliest
    ``ingested_at``. Two fetches that give one version (series, period and
    ``available_at``) different values, or the same value with a different
    number of decimals, disagree. ``Conflicts.REFUSE`` raises: that is a source
    changing what it published. ``Conflicts.LATEST`` keeps the value of the
    latest fetch, for a curated data set that corrects transcription errors in
    old vintages.
    """
    for t in tables:
        if t.name != name:
            raise VintageError(f"cannot put {t.name!r} into {name!r}")
    table = pa.concat_tables([t.table for t in tables]) if tables else _empty()
    ids = [SERIES, FREQUENCY, EVENT_TIME, AVAILABLE_AT]
    seen = table.group_by([*ids, VALUE, VALUE_SCALE]).aggregate([(INGESTED_AT, "min")])
    seen = seen.rename_columns([*ids, VALUE, VALUE_SCALE, INGESTED_AT])
    if seen.num_rows:
        counts = seen.group_by(ids).aggregate([([], "count_all")])
        if pc.max(counts.column("count_all")).as_py() > 1:
            if conflicts is Conflicts.REFUSE:
                raise VintageError(f"{name}: fetches disagree on the value of a published version")
            seen = _latest_value(table, seen, ids)
    return concat(name, [PitTable(name, KEYS, seen.select(_empty().column_names))])


def _latest_value(table: pa.Table, seen: pa.Table, ids: list[str]) -> pa.Table:
    """Of the values one version was given, keep the one from the latest fetch.

    Each value's latest sighting is the latest ``ingested_at`` among the rows that
    carry it; the value seen last wins, and keeps the earliest ``ingested_at`` of
    the fetches that showed that value (rows with ``seen``'s columns).
    """
    cols = [*ids, VALUE, VALUE_SCALE]
    last = table.group_by(cols).aggregate([(INGESTED_AT, "max")])
    last = last.rename_columns([*cols, "_last"])
    newest = last.group_by(ids).aggregate([("_last", "max")]).rename_columns([*ids, "_last"])
    winners = last.join(newest, keys=[*ids, "_last"], join_type="inner").select(cols)
    return seen.join(winners, keys=cols, join_type="inner")


def compact(t: PitTable) -> PitTable:
    """``t`` without the versions whose value equals the version before them."""
    order = (SERIES, EVENT_TIME, AVAILABLE_AT)
    table = t.table.sort_by([(c, "ascending") for c in order]).combine_chunks()
    if table.num_rows <= 1:
        return PitTable(t.name, t.keys, table)
    common = _common_mantissas(table)
    same_value = pc.equal(common[1:], common[:-1])
    names = table.column(SERIES).combine_chunks()
    periods = table.column(EVENT_TIME).combine_chunks()
    same_series = pc.equal(names[1:], names[:-1])
    same_period = pc.equal(periods[1:], periods[:-1])
    repeat = pc.and_(pc.and_(same_series, same_period), same_value)
    keep = pa.concat_arrays([pa.array([True]), pc.invert(repeat)])
    return PitTable(t.name, t.keys, table.filter(keep))


def revisions(t: PitTable, series: str, start: date) -> list[tuple[datetime, Decimal]]:
    """Every version of one observation, oldest first: when it was public and its value."""
    table = t.table.filter(
        pc.and_(
            pc.equal(t.table.column(SERIES), pa.scalar(series, pa.string())),
            pc.equal(
                t.table.column(EVENT_TIME),
                pa.scalar(utc(start.year, start.month, start.day), TIME_TYPE),
            ),
        )
    ).sort_by(AVAILABLE_AT)
    out: list[tuple[datetime, Decimal]] = []
    for row in table.select([AVAILABLE_AT, VALUE, VALUE_SCALE]).to_pylist():
        out.append((ensure_utc(row[AVAILABLE_AT]), Decimal(row[VALUE]).scaleb(-row[VALUE_SCALE])))
    return out


def _common_mantissas(table: pa.Table) -> Column:
    """Every value as a mantissa at the table's largest scale, so equal values compare equal."""
    scales = table.column(VALUE_SCALE).cast(MANTISSA_TYPE)
    top = pc.max(scales)
    factors = pc.power_checked(_TEN, pc.subtract(top, scales))
    try:
        return pc.multiply_checked(table.column(VALUE), factors).combine_chunks()
    except pa.ArrowInvalid as exc:
        raise VintageError(f"values too large to compare at {top.as_py()} decimals: {exc}") from exc


def _empty() -> pa.Table:
    return pa.table(
        {
            SERIES: pa.array([], pa.string()),
            FREQUENCY: pa.array([], pa.string()),
            EVENT_TIME: pa.array([], TIME_TYPE),
            AVAILABLE_AT: pa.array([], TIME_TYPE),
            INGESTED_AT: pa.array([], TIME_TYPE),
            VALUE: pa.array([], MANTISSA_TYPE),
            VALUE_SCALE: pa.array([], SCALE_TYPE),
        }
    )
