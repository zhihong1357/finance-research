# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Binance's funding rate history (Phase 2.5.6), from the public archive's monthly zip files.

Each zip in ``data/futures/um/monthly/fundingRate/<SYMBOL>/`` holds one CSV
named like the zip, with a header line and three columns
(docs/architecture.md 7.3)::

    calc_time, funding_interval_hours, last_funding_rate

``calc_time`` is the settlement instant in milliseconds since the epoch (UTC):
on the hour, or up to about a tenth of a second after it. ``last_funding_rate``
is the rate that was charged, decimal text kept exactly; small rates are
sometimes printed in scientific notation and are read exactly too. A file must
hold only settlements of its own month.
"""

from datetime import datetime

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data import csvtext, funding
from finresearch.data.fixed import FixedPointError, parse
from finresearch.data.manifest import ManifestEntry
from finresearch.data.table import PitTable
from finresearch.sources.binance_klines import period_bounds

EXCHANGE = "binance"
COLUMNS = ("calc_time", "funding_interval_hours", "last_funding_rate")
_MS = 1000  # microseconds per millisecond; milliseconds per second


class FundingFileError(ValueError):
    """A funding file that does not hold what its name and the format promise."""


def parse_funding(text: bytes, entry: ManifestEntry, ingested_at: datetime) -> PitTable:
    """The settlements in one funding CSV; ``ingested_at`` is when the archive published it."""
    header = text.lstrip().startswith(b"calc_time")
    try:
        table = csvtext.read(text, COLUMNS, {"calc_time", "funding_interval_hours"}, header=header)
    except csvtext.CsvError as exc:
        raise FundingFileError(f"{entry.url}: {exc}") from exc
    if table.num_rows:
        table = table.sort_by("calc_time")
    times = table.column("calc_time").combine_chunks()
    start, end = period_bounds(entry)
    lo, hi = int(start.timestamp()) * _MS, int(end.timestamp()) * _MS
    if table.num_rows and (pc.min(times).as_py() < lo or pc.max(times).as_py() >= hi):
        raise FundingFileError(f"{entry.url}: a settlement falls outside {entry.period}")
    try:
        return funding.funding(
            EXCHANGE,
            entry.symbol,
            pc.multiply(times, pa.scalar(_MS, pa.int64())),
            parse(table.column("last_funding_rate").combine_chunks(), exponents=True),
            table.column("funding_interval_hours").combine_chunks(),
            ensure_utc(ingested_at),
        )
    except (funding.FundingError, FixedPointError) as exc:
        raise FundingFileError(f"{entry.url}: {exc}") from exc
