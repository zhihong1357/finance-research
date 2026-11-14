# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Binance's 5-minute open interest and long/short ratios (Phase 2.5.6, decision 0011).

Each zip in ``data/futures/um/daily/metrics/<SYMBOL>/`` holds one CSV named
like the zip, with a header line and eight columns (docs/architecture.md 7.3)::

    create_time, symbol, sum_open_interest, sum_open_interest_value,
    count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
    count_long_short_ratio, sum_taker_long_short_vol_ratio

What the files are like, measured on the archive:

* ``create_time`` is text, ``YYYY-MM-DD HH:MM:SS`` in UTC, normally on a
  five-minute mark. **What it labels depends on when Binance generated the
  file**: in files published up to 2025 the reading was taken at
  ``create_time``; in files published from July 2026 on (every day since
  2024-03-04 has been regenerated) it was taken five minutes *after*
  ``create_time``. The label is kept as it is and the reading is made
  available one period after it, which is right or late in both cases and
  never early (decision 0011).
* A day's file holds 288 readings: labelled 00:00 to 23:55 in most files, 00:05
  to the next midnight in others. A reading labelled in the next month belongs
  to the next month's partition (``u1.Product.edge_days``).
* Rows are not always in order, and some files hold every row twice; identical
  rows are merged, two different rows for one time are an error.
* Numbers are padded with zeros to 8 or 16 decimals; the padding is dropped.
  Zero is sometimes printed as ``0E-16``. The ratios are blank when Binance
  had none.
* ``sum_taker_long_short_vol_ratio`` is not kept: it is the taker buy volume
  over the taker sell volume of five minutes, which the 1-minute bars give
  exactly, and in files of early 2022 it matches neither the five minutes
  before nor the five minutes after its label.
"""

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data import csvtext, open_interest
from finresearch.data.fixed import FixedPointError, parse
from finresearch.data.manifest import ManifestEntry
from finresearch.data.table import PitTable

EXCHANGE = "binance"
COLUMNS = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)
# Source column of each kept value.
SOURCE = {
    "open_interest": "sum_open_interest",
    "open_interest_value": "sum_open_interest_value",
    "top_trader_account_ratio": "count_toptrader_long_short_ratio",
    "top_trader_position_ratio": "sum_toptrader_long_short_ratio",
    "account_ratio": "count_long_short_ratio",
}
PERIOD = timedelta(minutes=5)  # the spacing of the readings, and the most a label runs early
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_US = 1_000_000  # microseconds per second


class MetricsFileError(ValueError):
    """A metrics file that does not hold what its name and the format promise."""


def parse_metrics(text: bytes, entry: ManifestEntry, ingested_at: datetime) -> PitTable:
    """The readings in one metrics CSV; ``ingested_at`` is when the archive published it."""
    header = text.lstrip().startswith(b"create_time")
    try:
        table = csvtext.read(text, COLUMNS, set(), header=header)
    except csvtext.CsvError as exc:
        raise MetricsFileError(f"{entry.url}: {exc}") from exc
    if (
        table.num_rows
        and not pc.all(pc.equal(table.column("symbol"), pa.scalar(entry.symbol))).as_py()
    ):
        raise MetricsFileError(f"{entry.url}: a row is not {entry.symbol}")
    try:
        stamps = pc.strptime(table.column("create_time"), format=_TIME_FORMAT, unit="us")
    except pa.ArrowInvalid as exc:
        raise MetricsFileError(f"{entry.url}: a create_time is not a time: {exc}") from exc
    kept = pa.table(
        {"label": stamps.cast(pa.int64()), **{c: table.column(c) for c in SOURCE.values()}}
    )
    # Identical rows collapse into one; what is left must have one row per time.
    kept = kept.group_by(kept.column_names, use_threads=False).aggregate([]).sort_by("label")
    labels = kept.column("label").combine_chunks()
    start = datetime.fromisoformat(entry.period).replace(tzinfo=UTC)
    lo, hi = int(start.timestamp()) * _US, int((start + timedelta(days=1)).timestamp()) * _US
    # Files that label a reading with the time it was taken run from 00:05 to the next
    # midnight (a little past it where the labels are off the five-minute marks).
    late = hi + int(PERIOD.total_seconds()) * _US
    if kept.num_rows and (pc.min(labels).as_py() < lo or pc.max(labels).as_py() >= late):
        raise MetricsFileError(f"{entry.url}: a row falls outside {entry.period}")
    try:
        values = {
            name: parse(
                kept.column(column).combine_chunks(),
                exponents=True,
                trim=True,
                blanks=name in open_interest.RATIOS,
            )
            for name, column in SOURCE.items()
        }
        return open_interest.readings(
            EXCHANGE,
            entry.symbol,
            labels,
            int(PERIOD.total_seconds()) * _US,
            values,
            ensure_utc(ingested_at),
        )
    except (open_interest.OpenInterestError, FixedPointError) as exc:
        raise MetricsFileError(f"{entry.url}: {exc}") from exc
