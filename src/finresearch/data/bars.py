# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""The 1-minute bars of research universe U1: one table shape for every exchange (decision 0010).

A row is one minute of one contract:

* keys ``exchange`` and ``symbol``. The symbol is the archive's folder name,
  which keeps a reused symbol's old and new contracts apart;
* ``event_time`` is the minute's start, ``available_at`` its end (the bar is
  complete only then), and ``ingested_at`` the moment the archive published
  the file the bar came from;
* prices ``open``, ``high``, ``low``, ``close`` and quantities ``volume`` (base
  asset), ``quote_volume`` (quote asset), ``taker_buy_volume`` and
  ``taker_buy_quote_volume``, each an exact fixed-point mantissa
  (``data/fixed.py``) with its scale in ``<name>_scale``;
* ``trades``, the number of trades.

Taker sell volume is ``volume - taker_buy_volume``; the average traded price is
``quote_volume / volume``.

``bars`` builds a table and refuses malformed input (misaligned or repeated
minutes, columns of different lengths). ``problems`` reports bars that are
well formed but impossible, such as a low above the high: what to do about
them is the processing job's decision, not the table's.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data.fixed import SCALE_TYPE, Column, Fixed, FixedPointError
from finresearch.data.table import AVAILABLE_AT, EVENT_TIME, INGESTED_AT, TIME_TYPE, PitTable

NAME = "u1.bars_1m"
KEYS = ("exchange", "symbol")
PRICES = ("open", "high", "low", "close")
QUANTITIES = ("volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume")
DECIMALS = PRICES + QUANTITIES
TRADES = "trades"
BAR = timedelta(minutes=1)
_BAR_US = 60_000_000  # one minute in microseconds
_ZERO = pa.scalar(0, pa.int64())
type Values = pa.Array[Any] | pa.ChunkedArray[Any]


class BarError(ValueError):
    """Input that cannot form a bar table."""


def scale_column(name: str) -> str:
    return f"{name}_scale"


def bars(
    exchange: str,
    symbol: str,
    start_us: Column,
    values: Mapping[str, Fixed],
    trades: Column,
    ingested_at: datetime,
) -> PitTable:
    """One contract's bars: ``start_us`` are minute starts in microseconds since the epoch, UTC."""
    n = len(start_us)
    if set(values) != set(DECIMALS):
        raise BarError(f"need exactly {DECIMALS}, got {sorted(values)}")
    lengths = {len(v) for v in values.values()} | {len(trades)}
    if lengths != {n}:
        raise BarError(f"columns of different lengths: {sorted(lengths | {n})}")
    starts = start_us.cast(pa.int64())
    if starts.null_count or trades.null_count:
        raise BarError("missing minute starts or trade counts")
    if n:
        # Integer division truncates; times after 1970 are positive, so this is the remainder.
        whole = pc.multiply(pc.divide(starts, _BAR_US), _BAR_US)
        if pc.any(pc.less(starts, _ZERO)).as_py() or pc.any(pc.not_equal(starts, whole)).as_py():
            raise BarError(f"{symbol}: a bar does not start on a whole minute")
        steps = pc.subtract(starts[1:], starts[:-1]) if n > 1 else pa.array([], pa.int64())
        if pc.any(pc.less_equal(steps, _ZERO)).as_py():
            raise BarError(f"{symbol}: minutes are repeated or out of order")
    event = starts.cast(TIME_TYPE)
    columns: dict[str, Any] = {
        "exchange": pa.array([exchange] * n, type=pa.string()),
        "symbol": pa.array([symbol] * n, type=pa.string()),
        EVENT_TIME: event,
        AVAILABLE_AT: pc.add(starts, _BAR_US).cast(TIME_TYPE),
        INGESTED_AT: pa.array([ensure_utc(ingested_at)] * n, type=TIME_TYPE),
    }
    for name in DECIMALS:
        columns[name] = values[name].mantissa
        columns[scale_column(name)] = pa.array([values[name].scale] * n, type=SCALE_TYPE)
    columns[TRADES] = trades.cast(pa.int64())
    return PitTable(NAME, KEYS, pa.table(columns))


def from_trades(
    exchange: str,
    symbol: str,
    time_us: Column,
    price: Fixed,
    size: Fixed,
    taker_buys: Column,
    ingested_at: datetime,
) -> PitTable:
    """1-minute bars from individual trades, already in time order (ties in trade order).

    ``time_us`` is each trade's time in microseconds since the epoch (UTC),
    ``taker_buys`` is true where the taker bought. Open and close are the first
    and last trades of the minute; quote volume is the exact sum of price times
    size. Minutes without trades have no bar. Sums run in 128-bit decimals and
    must fit in 64-bit mantissas again, or ``FixedPointError`` is raised.
    """
    n = len(time_us)
    if not (len(price) == len(size) == len(taker_buys) == n):
        raise BarError("trade columns of different lengths")
    if n and pc.any(pc.less(pc.subtract(time_us[1:], time_us[:-1]), _ZERO)).as_py():
        raise BarError(f"{symbol}: trades are not in time order")
    try:
        quote = pc.multiply_checked(price.mantissa, size.mantissa)
    except pa.ArrowInvalid as exc:
        raise FixedPointError(f"{symbol}: price times size does not fit in 64 bits") from exc
    minute = pc.multiply(pc.divide(time_us.cast(pa.int64()), _BAR_US), _BAR_US)
    wide = pa.decimal128(38, 0)
    zero = pa.scalar(0, pa.int64())
    trades = pa.table(
        {
            "minute": minute,
            "price": price.mantissa,
            "size": size.mantissa.cast(wide),
            "quote": quote.cast(wide),
            "buy": pc.if_else(taker_buys, size.mantissa, zero).cast(wide),
            "buy_quote": pc.if_else(taker_buys, quote, zero).cast(wide),
        }
    )
    # Only aggregates that do not depend on row order are left to the grouping, and its
    # result is sorted: a hash grouping returns its groups in no promised order (on real
    # files it did not return them in order of first appearance).
    g = (
        trades.group_by("minute", use_threads=False)
        .aggregate(
            [
                ("price", "max"),
                ("price", "min"),
                ("size", "sum"),
                ("quote", "sum"),
                ("buy", "sum"),
                ("buy_quote", "sum"),
                ("price", "count"),
            ]
        )
        .sort_by("minute")
    )
    # Trades are in time order, so each minute is one run of rows: its first row opens
    # the bar and its last row closes it.
    if n:
        changes = pc.indices_nonzero(pc.not_equal(minute[1:], minute[:-1])).cast(pa.int64())
        first = pa.concat_arrays([pa.array([0], pa.int64()), pc.add(changes, 1)])
        last = pa.concat_arrays([changes, pa.array([n - 1], pa.int64())])
    else:
        first = last = pa.array([], pa.int64())
    starts = pc.take(minute, first)
    if not starts.equals(g.column("minute").combine_chunks()):
        raise BarError(f"{symbol}: the grouping lost or invented a minute")

    def narrow(column: str, scale: int) -> Fixed:
        try:
            return Fixed(g.column(column).combine_chunks().cast(pa.int64()), scale)
        except pa.ArrowInvalid as exc:
            raise FixedPointError(f"{symbol}: a minute's {column} does not fit in 64 bits") from exc

    q_scale = price.scale + size.scale
    values = {
        "open": Fixed(pc.take(price.mantissa, first), price.scale),
        "high": Fixed(g.column("price_max").combine_chunks(), price.scale),
        "low": Fixed(g.column("price_min").combine_chunks(), price.scale),
        "close": Fixed(pc.take(price.mantissa, last), price.scale),
        "volume": narrow("size_sum", size.scale),
        "quote_volume": narrow("quote_sum", q_scale),
        "taker_buy_volume": narrow("buy_sum", size.scale),
        "taker_buy_quote_volume": narrow("buy_quote_sum", q_scale),
    }
    return bars(
        exchange,
        symbol,
        starts,
        values,
        g.column("price_count").combine_chunks(),
        ingested_at,
    )


def concat(tables: Sequence[PitTable]) -> PitTable:
    """Bar tables of one contract (for example one per day) as one table."""
    return PitTable(NAME, KEYS, pa.concat_tables([t.table for t in tables]))


def _common(table: pa.Table, names: tuple[str, ...]) -> list[Values]:
    """The mantissas of ``names`` brought to one scale per row, so they compare exactly."""
    scales = [table.column(scale_column(c)) for c in names]
    top = scales[0]
    for s in scales[1:]:
        top = pc.max_element_wise(top, s)
    out: list[Values] = []
    for c, s in zip(names, scales, strict=True):
        factor = pc.power(pa.scalar(10, pa.int64()), pc.cast(pc.subtract(top, s), pa.int64()))
        out.append(pc.multiply_checked(table.column(c), factor))
    return out


def problems(t: PitTable) -> list[str]:
    """Bars that are well formed but impossible, one line per kind with a count and an example."""
    table = t.table
    if table.num_rows == 0:
        return []
    o, h, low, c = _common(table, PRICES)
    vol, quote, buy, buy_quote = (table.column(q) for q in QUANTITIES)
    vol_buy = _common(table, ("volume", "taker_buy_volume"))
    quote_buy = _common(table, ("quote_volume", "taker_buy_quote_volume"))
    trades = table.column(TRADES)
    zero = _ZERO
    checks = {
        "high below open, close or low": pc.or_(
            pc.or_(pc.less(h, o), pc.less(h, c)), pc.less(h, low)
        ),
        "low above open or close": pc.or_(pc.greater(low, o), pc.greater(low, c)),
        "negative quantity": pc.or_(
            pc.or_(pc.less(vol, zero), pc.less(quote, zero)),
            pc.or_(pc.less(buy, zero), pc.less(buy_quote, zero)),
        ),
        "taker buy volume above volume": pc.greater(vol_buy[1], vol_buy[0]),
        "taker buy quote volume above quote volume": pc.greater(quote_buy[1], quote_buy[0]),
        "negative trade count": pc.less(trades, zero),
        "volume without trades": pc.and_(pc.equal(trades, zero), pc.greater(vol, zero)),
        "trades without volume": pc.and_(pc.greater(trades, zero), pc.equal(vol, zero)),
    }
    found: list[str] = []
    for label, mask in checks.items():
        count = int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)
        if count:
            first = table.filter(mask).slice(0, 1).to_pylist()[0]
            found.append(f"{label}: {count} bar(s), first {first['symbol']} {first[EVENT_TIME]}")
    return found


COMPARED = (*DECIMALS, TRADES)


def _same(a: Values, sa: Values, b: Values, sb: Values) -> Values:
    """Where two fixed-point columns hold the same value, whatever their scales."""
    top = pc.max_element_wise(sa, sb)
    ten = pa.scalar(10, pa.int64())
    left = pc.multiply_checked(a, pc.power(ten, pc.cast(pc.subtract(top, sa), pa.int64())))
    right = pc.multiply_checked(b, pc.power(ten, pc.cast(pc.subtract(top, sb), pa.int64())))
    return pc.equal(left, right)


def compare(reference: PitTable, other: PitTable) -> dict[str, int]:
    """How two bar tables of one contract disagree, minute by minute and field by field.

    ``reference`` (Binance's klines) also lists minutes without trades, with a
    trade count of 0; a table built from trades has no row for them, so those
    count as agreeing. The counts: ``minutes`` with trades in either table,
    ``missing`` (trades in ``reference``, no bar in ``other``), ``extra`` (a bar
    in ``other``, no trades in ``reference``), for each field the number of
    minutes in both tables where its exact values differ, and ``differing``,
    the minutes in both where any field differs.
    """
    ref = reference.table.filter(pc.greater(reference.table.column(TRADES), _ZERO))
    oth = other.table

    def side(t: pa.Table, tag: str) -> pa.Table:
        cols: dict[str, Any] = {
            EVENT_TIME: t.column(EVENT_TIME),
            f"{tag}present": pa.array([True] * t.num_rows),
        }
        for name in DECIMALS:
            cols[f"{tag}{name}"] = t.column(name)
            cols[f"{tag}{name}_s"] = t.column(scale_column(name))
        cols[f"{tag}{TRADES}"] = t.column(TRADES)
        return pa.table(cols)

    joined = side(ref, "r_").join(side(oth, "o_"), keys=EVENT_TIME, join_type="full outer")
    in_ref = pc.fill_null(joined.column("r_present"), pa.scalar(False))
    in_oth = pc.fill_null(joined.column("o_present"), pa.scalar(False))
    both = joined.filter(pc.and_(in_ref, in_oth))
    counts = {
        "minutes": int(joined.num_rows),
        "missing": int(
            pc.sum(pc.cast(pc.and_(in_ref, pc.invert(in_oth)), pa.int64())).as_py() or 0
        ),
        "extra": int(pc.sum(pc.cast(pc.and_(in_oth, pc.invert(in_ref)), pa.int64())).as_py() or 0),
    }
    any_field = pc.not_equal(both.column(f"r_{TRADES}"), both.column(f"o_{TRADES}"))
    counts[TRADES] = int(pc.sum(pc.cast(any_field, pa.int64())).as_py() or 0)
    for name in DECIMALS:
        differs = pc.invert(
            _same(
                both.column(f"r_{name}"), both.column(f"r_{name}_s"),
                both.column(f"o_{name}"), both.column(f"o_{name}_s"),
            )
        )  # fmt: skip
        counts[name] = int(pc.sum(pc.cast(differs, pa.int64())).as_py() or 0)
        any_field = pc.or_(any_field, differs)
    counts["differing"] = int(pc.sum(pc.cast(any_field, pa.int64())).as_py() or 0)
    return counts
