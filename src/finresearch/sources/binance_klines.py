# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Binance's official 1-minute klines (decision 0010), from the public archive's zip files.

Each zip in ``data/futures/um/{daily,monthly}/klines/<SYMBOL>/1m/`` holds one CSV
named like the zip. Newer files start with a header line, older ones do not.
The columns are fixed (docs/architecture.md 7.3)::

    open_time, open, high, low, close, volume, close_time, quote_volume,
    count, taker_buy_volume, taker_buy_quote_volume, ignore

Times are milliseconds since the epoch (UTC); prices and quantities are decimal
text and are kept exactly (``data/fixed.py``). A file must cover only its own
day or month, with ``close_time`` one millisecond before the next minute.
"""

import io
import zipfile
from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data import bars, csvtext
from finresearch.data.fixed import Fixed, FixedPointError, parse
from finresearch.data.manifest import ManifestEntry
from finresearch.data.table import PitTable

EXCHANGE = "binance"
COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)
_SOURCE = {  # bar column <- kline column
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "quote_volume": "quote_volume",
    "taker_buy_volume": "taker_buy_volume",
    "taker_buy_quote_volume": "taker_buy_quote_volume",
}
_MS = 1000  # microseconds per millisecond
_CLOSE_OFFSET_MS = 59_999  # a bar closes one millisecond before the next one opens


class KlineError(ValueError):
    """A kline file that does not hold what its name and the format promise."""


def csv_from_zip(content: bytes, entry: ManifestEntry) -> bytes:
    """The one CSV inside an archive zip, which must be named after the zip."""
    expected = entry.url.rsplit("/", 1)[-1].removesuffix(".zip") + ".csv"
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if z.namelist() != [expected]:
                raise KlineError(f"{entry.url}: holds {z.namelist()[:3]}, expected {expected}")
            return z.read(expected)
    except zipfile.BadZipFile as exc:
        raise KlineError(f"{entry.url}: not a valid zip: {exc}") from exc


def _period(entry: ManifestEntry) -> tuple[datetime, datetime]:
    """The UTC interval ``[start, end)`` a daily (YYYY-MM-DD) or monthly (YYYY-MM) file covers."""
    parts = [int(p) for p in entry.period.split("-")]
    if len(parts) == 3:  # noqa: PLR2004 - year, month, day
        day = date(*parts)
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return start, datetime.fromordinal(day.toordinal() + 1).replace(tzinfo=UTC)
    year, month = parts
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)
    return start, end


def parse_klines(text: bytes, entry: ManifestEntry, ingested_at: datetime) -> PitTable:
    """The bars in one kline CSV; ``ingested_at`` is when the archive published the file."""
    header = text.lstrip().startswith(b"open_time")
    try:
        table = csvtext.read(text, COLUMNS, {"open_time", "close_time", "count"}, header=header)
    except csvtext.CsvError as exc:
        raise KlineError(f"{entry.url}: {exc}") from exc
    if table.num_rows:
        table = table.sort_by("open_time")
    opens = table.column("open_time").combine_chunks()
    expected_close = pc.add(opens, pa.scalar(_CLOSE_OFFSET_MS, pa.int64()))
    if not pc.all(pc.equal(table.column("close_time"), expected_close)).as_py() and table.num_rows:
        raise KlineError(f"{entry.url}: a close_time is not open_time + 59,999 ms")
    start, end = _period(entry)
    lo, hi = int(start.timestamp() * _MS), int(end.timestamp() * _MS)
    if table.num_rows and (pc.min(opens).as_py() < lo or pc.max(opens).as_py() >= hi):
        raise KlineError(f"{entry.url}: a bar falls outside {entry.period}")
    try:
        values: dict[str, Fixed] = {
            bar: parse(table.column(src).combine_chunks()) for bar, src in _SOURCE.items()
        }
    except FixedPointError as exc:
        raise KlineError(f"{entry.url}: {exc}") from exc
    try:
        return bars.bars(
            EXCHANGE,
            entry.symbol,
            pc.multiply(opens, pa.scalar(_MS, pa.int64())),
            values,
            table.column("count").combine_chunks(),
            ensure_utc(ingested_at),
        )
    except bars.BarError as exc:
        raise KlineError(f"{entry.url}: {exc}") from exc
