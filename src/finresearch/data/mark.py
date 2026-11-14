# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Mark prices of research universe U1, one per minute (Phase 2.5.6, decision 0010).

The *mark price* is the price an exchange uses to value open positions and to
decide liquidations: not the last trade, but a smoothed price built from the
spot index and the contract's own order book, so that one odd trade cannot
liquidate anyone. A row is one minute of one contract:

* keys ``exchange`` and ``symbol`` (the archive's folder name, as in the bars);
* ``event_time`` is the minute's start, ``available_at`` its end, and
  ``ingested_at`` the moment the archive published the file;
* ``close`` is the mark price at the end of the minute, an exact fixed-point
  mantissa (``data/fixed.py``) with its scale in ``close_scale``. Only the
  close is kept (decision 0010: all four prices would take four times the
  space);
* ``samples`` is how many mark prices the exchange recorded in the minute,
  normally 60. A minute with 0 samples carries the previous close forward:
  the price in that row is stale, and research must be able to tell.
"""

from collections.abc import Sequence
from datetime import datetime

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.data import bars
from finresearch.data.fixed import SCALE_TYPE, Column, Fixed
from finresearch.data.table import EVENT_TIME, PitTable

NAME = "u1.mark_1m"
KEYS = ("exchange", "symbol")
CLOSE = "close"
CLOSE_SCALE = "close_scale"
SAMPLES = "samples"
SAMPLES_TYPE = pa.int16()
_ZERO = pa.scalar(0, pa.int64())


class MarkError(ValueError):
    """Input that cannot form a mark price table."""


def marks(
    exchange: str,
    symbol: str,
    start_us: Column,
    close: Fixed,
    samples: Column,
    ingested_at: datetime,
) -> PitTable:
    """One contract's mark prices; ``start_us`` are minute starts in microseconds, ascending."""
    n = len(start_us)
    if not (len(close) == len(samples) == n):
        raise MarkError(f"{symbol}: columns of different lengths")
    if samples.null_count:
        raise MarkError(f"{symbol}: missing sample counts")
    try:
        columns = bars.minute_columns(exchange, symbol, start_us, ingested_at)
        columns[SAMPLES] = samples.cast(SAMPLES_TYPE)
    except (bars.BarError, pa.ArrowInvalid) as exc:
        raise MarkError(str(exc)) from exc
    columns[CLOSE] = close.mantissa
    columns[CLOSE_SCALE] = pa.array([close.scale] * n, type=SCALE_TYPE)
    return PitTable(NAME, KEYS, pa.table(columns))


def concat(tables: Sequence[PitTable]) -> PitTable:
    """Mark price tables of one contract (for example one per day) as one table."""
    return PitTable(NAME, KEYS, pa.concat_tables([t.table for t in tables]))


def problems(t: PitTable) -> list[str]:
    """Rows that are well formed but impossible, one line per kind with a count and an example."""
    table = t.table
    if table.num_rows == 0:
        return []
    checks = {
        "mark price not positive": pc.less_equal(table.column(CLOSE), _ZERO),
        "negative sample count": pc.less(table.column(SAMPLES), pa.scalar(0, SAMPLES_TYPE)),
    }
    found: list[str] = []
    for label, mask in checks.items():
        count = int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)
        if count:
            first = table.filter(mask).slice(0, 1).to_pylist()[0]
            found.append(f"{label}: {count} row(s), first {first['symbol']} {first[EVENT_TIME]}")
    return found
