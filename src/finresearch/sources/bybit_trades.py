# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Bybit's archived trades as 1-minute bars (decision 0010: Bybit has no klines in its archive).

Each archive file is one contract-day of trades, a gzip CSV whose first line
names the columns (a column was added in 2025, so columns are found by name).
The ones used here:

* ``timestamp``: seconds since the epoch with up to four decimals, kept exactly
  and turned into microseconds;
* ``symbol``: must be the symbol in the file name on every row;
* ``side``: the taker's side, ``Buy`` or ``Sell`` (Bybit documents the public
  trade feed's ``side`` as the taker's side; the archive holds that feed);
* ``size`` and ``price``: decimal text, kept exactly; large and small values are
  sometimes printed in scientific notation (``1.0985e+06``), read exactly too.

Older files list trades newest first, newer ones oldest first. Trades are put
in time order; trades with the same timestamp keep their order in the file,
read oldest first, so the first and last price of a minute are well defined.
"""

import gzip
import zlib
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data import bars, csvtext
from finresearch.data.fixed import Fixed, FixedPointError, parse, rescale
from finresearch.data.manifest import ManifestEntry
from finresearch.data.table import PitTable
from finresearch.sources.bybit_archive import FILE_NAME

EXCHANGE = "bybit"
COLUMNS = ("timestamp", "symbol", "side", "size", "price")
_SIDES = ("Buy", "Sell")
_MICRO = 6  # decimal places of a microsecond


class TradeError(ValueError):
    """A trade file that does not hold what its name and the format promise."""


def parse_trades(content: bytes, entry: ManifestEntry, ingested_at: datetime) -> PitTable:
    """The 1-minute bars of one archived day of trades (``content`` is the gzip file)."""
    name = entry.url.rsplit("/", 1)[-1]
    m = FILE_NAME.fullmatch(name)
    if m is None:
        raise TradeError(f"{entry.url}: not a day file name")
    traded = m["symbol"]
    try:
        text = gzip.decompress(content)
        table = csvtext.read_named(text, COLUMNS)
    except (OSError, EOFError, zlib.error, csvtext.CsvError) as exc:
        raise TradeError(f"{entry.url}: {type(exc).__name__}: {exc}") from exc
    if table.num_rows == 0:
        raise TradeError(f"{entry.url}: no trades")
    if not pc.all(pc.equal(table.column("symbol"), pa.scalar(traded))).as_py():
        raise TradeError(f"{entry.url}: a row is not {traded}")
    if not pc.all(pc.is_in(table.column("side"), value_set=pa.array(_SIDES))).as_py():
        raise TradeError(f"{entry.url}: a side is neither Buy nor Sell")
    try:
        stamps = parse(table.column("timestamp").combine_chunks())
        time_us = rescale(stamps, _MICRO) if stamps.scale <= _MICRO else None
        # Bybit prints some sizes and prices as Python prints floats (1.0985e+06).
        price = parse(table.column("price").combine_chunks(), exponents=True)
        size = parse(table.column("size").combine_chunks(), exponents=True)
    except FixedPointError as exc:
        raise TradeError(f"{entry.url}: {exc}") from exc
    if time_us is None:
        raise TradeError(f"{entry.url}: timestamps finer than a microsecond")
    start = datetime.fromisoformat(entry.period).replace(tzinfo=UTC)
    lo = int(start.timestamp()) * 10**_MICRO
    hi = int((start + timedelta(days=1)).timestamp()) * 10**_MICRO
    if pc.min(time_us).as_py() < lo or pc.max(time_us).as_py() >= hi:
        raise TradeError(f"{entry.url}: a trade falls outside {entry.period} (UTC)")
    # Oldest first in file order, then by time: equal times keep that order.
    n = table.num_rows
    newest_first = time_us[0].as_py() > time_us[n - 1].as_py()
    order = pa.array(range(n - 1, -1, -1) if newest_first else range(n), type=pa.int64())
    keyed = pa.table({"t": pc.take(time_us, order), "i": pa.array(range(n), type=pa.int64())})
    rows = pc.take(
        order, pc.sort_indices(keyed, sort_keys=[("t", "ascending"), ("i", "ascending")])
    )
    try:
        return bars.from_trades(
            EXCHANGE,
            entry.symbol,
            pc.take(time_us, rows),
            Fixed(pc.take(price.mantissa, rows), price.scale),
            Fixed(pc.take(size.mantissa, rows), size.scale),
            pc.equal(pc.take(table.column("side").combine_chunks(), rows), pa.scalar("Buy")),
            ensure_utc(ingested_at),
        )
    except (bars.BarError, FixedPointError) as exc:
        raise TradeError(f"{entry.url}: {exc}") from exc
