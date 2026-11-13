# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Binance's archived trades as 1-minute bars, to check the official klines against (2.5.5).

Decision 0010 takes Binance's 1-minute data from its klines and keeps the
trades for checking them. Each zip in ``data/futures/um/daily/trades/<SYMBOL>/``
holds one CSV of a contract-day's trades; newer files start with a header::

    id, price, qty, quote_qty, time, is_buyer_maker

``time`` is in milliseconds; the taker bought when ``is_buyer_maker`` is false.
Trades are put in order of time, then id. Large quantities are sometimes
printed in scientific notation (``1.7458208E7``); they are read exactly.
"""

from datetime import datetime

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data import bars, csvtext
from finresearch.data.fixed import FixedPointError, parse
from finresearch.data.manifest import ManifestEntry
from finresearch.data.table import PitTable
from finresearch.sources.binance_klines import csv_from_zip

EXCHANGE = "binance"
COLUMNS = ("id", "price", "qty", "quote_qty", "time", "is_buyer_maker")
_MS = 1000  # microseconds per millisecond


class TradeError(ValueError):
    """A trade file that does not hold what its name and the format promise."""


def parse_trades(content: bytes, entry: ManifestEntry, ingested_at: datetime) -> PitTable:
    """The 1-minute bars of one archived day of trades (``content`` is the zip)."""
    text = csv_from_zip(content, entry)
    header = text.lstrip().startswith(b"id,")
    try:
        table = csvtext.read(text, COLUMNS, {"id", "time"}, header=header)
    except csvtext.CsvError as exc:
        raise TradeError(f"{entry.url}: {exc}") from exc
    if table.num_rows == 0:
        raise TradeError(f"{entry.url}: no trades")
    maker = table.column("is_buyer_maker")
    if not pc.all(pc.is_in(maker, value_set=pa.array(["true", "false"]))).as_py():
        raise TradeError(f"{entry.url}: is_buyer_maker is neither true nor false")
    table = table.sort_by([("time", "ascending"), ("id", "ascending")])
    try:
        # Large quantities are sometimes printed in scientific notation (1.7458208E7).
        price = parse(table.column("price").combine_chunks(), exponents=True)
        qty = parse(table.column("qty").combine_chunks(), exponents=True)
        return bars.from_trades(
            EXCHANGE,
            entry.symbol,
            pc.multiply(table.column("time").combine_chunks(), pa.scalar(_MS, pa.int64())),
            price,
            qty,
            pc.equal(table.column("is_buyer_maker").combine_chunks(), pa.scalar("false")),
            ensure_utc(ingested_at),
        )
    except (FixedPointError, bars.BarError) as exc:
        raise TradeError(f"{entry.url}: {exc}") from exc
