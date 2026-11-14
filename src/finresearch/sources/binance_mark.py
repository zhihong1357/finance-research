# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Binance's 1-minute mark price klines (Phase 2.5.6), from the public archive's zip files.

Each zip in ``data/futures/um/{daily,monthly}/markPriceKlines/<SYMBOL>/1m/``
holds one CSV in the layout of the trade klines (``binance_klines.COLUMNS``),
with or without a header line. A mark price has no volume, so the volume
columns are zero; ``count`` is the number of mark prices recorded in the
minute (normally 60, one a second), and a minute with a count of 0 repeats the
previous close (docs/architecture.md 7.3).

Only ``open_time``, ``close`` and ``count`` are kept (decision 0010). A file
must cover only its own day or month, with ``close_time`` one millisecond
before the next minute.
"""

from datetime import datetime

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data import csvtext, mark
from finresearch.data.fixed import FixedPointError, parse
from finresearch.data.manifest import ManifestEntry
from finresearch.data.table import PitTable
from finresearch.sources.binance_klines import COLUMNS, period_bounds

EXCHANGE = "binance"
_MS = 1000  # microseconds per millisecond; milliseconds per second
_CLOSE_OFFSET_MS = 59_999  # a bar closes one millisecond before the next one opens


class MarkFileError(ValueError):
    """A mark price file that does not hold what its name and the format promise."""


def parse_mark(text: bytes, entry: ManifestEntry, ingested_at: datetime) -> PitTable:
    """The mark prices in one kline CSV; ``ingested_at`` is when the archive published it."""
    header = text.lstrip().startswith(b"open_time")
    try:
        table = csvtext.read(text, COLUMNS, {"open_time", "close_time", "count"}, header=header)
    except csvtext.CsvError as exc:
        raise MarkFileError(f"{entry.url}: {exc}") from exc
    if table.num_rows:
        table = table.sort_by("open_time")
    opens = table.column("open_time").combine_chunks()
    expected_close = pc.add(opens, pa.scalar(_CLOSE_OFFSET_MS, pa.int64()))
    if table.num_rows and not pc.all(pc.equal(table.column("close_time"), expected_close)).as_py():
        raise MarkFileError(f"{entry.url}: a close_time is not open_time + 59,999 ms")
    start, end = period_bounds(entry)
    lo, hi = int(start.timestamp()) * _MS, int(end.timestamp()) * _MS
    if table.num_rows and (pc.min(opens).as_py() < lo or pc.max(opens).as_py() >= hi):
        raise MarkFileError(f"{entry.url}: a minute falls outside {entry.period}")
    try:
        return mark.marks(
            EXCHANGE,
            entry.symbol,
            pc.multiply(opens, pa.scalar(_MS, pa.int64())),
            parse(table.column("close").combine_chunks()),
            table.column("count").combine_chunks(),
            ensure_utc(ingested_at),
        )
    except (mark.MarkError, FixedPointError) as exc:
        raise MarkFileError(f"{entry.url}: {exc}") from exc
