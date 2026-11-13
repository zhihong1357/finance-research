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

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data.fixed import SCALE_TYPE, Column, Fixed
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
