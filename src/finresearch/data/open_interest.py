# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Open interest and long/short ratios of research universe U1 (Phase 2.5.6, decision 0011).

*Open interest* is how many contracts are open: every open long is matched by
an open short, and the count goes up when a new buyer meets a new seller and
down when both close. It measures how much money is positioned, where volume
measures how much changed hands. A row is one reading of one contract, every
five minutes:

* keys ``exchange`` and ``symbol`` (the archive's folder name, as in the bars);
* ``event_time`` is the time the source *labels* the reading with. The reading
  was taken at that time or up to one period later, depending on how the
  source generated the file (decision 0011); the table does not guess which;
* ``available_at`` is therefore the label plus one period: by then the reading
  existed under either meaning of the label, so nothing is seen early;
* ``ingested_at`` is the moment the archive published the file;
* ``open_interest`` is the number of open contracts in the contract's base
  asset and ``open_interest_value`` their value in the quote currency, exact
  fixed-point mantissas (``data/fixed.py``) with a ``_scale`` column each;
* three long/short ratios, each a quotient and so not an exact decimal, kept
  with every digit the source printed and null where the source left it out:
  ``top_trader_account_ratio`` (among the largest traders, accounts net long
  over accounts net short), ``top_trader_position_ratio`` (among the largest
  traders, long positions over short positions) and ``account_ratio`` (all
  accounts net long over all accounts net short).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data.fixed import SCALE_TYPE, Column, Fixed
from finresearch.data.table import AVAILABLE_AT, EVENT_TIME, INGESTED_AT, TIME_TYPE, PitTable

NAME = "u1.open_interest_5m"
KEYS = ("exchange", "symbol")
QUANTITIES = ("open_interest", "open_interest_value")  # always present
RATIOS = ("top_trader_account_ratio", "top_trader_position_ratio", "account_ratio")  # may be null
DECIMALS = (*QUANTITIES, *RATIOS)
_ZERO = pa.scalar(0, pa.int64())


class OpenInterestError(ValueError):
    """Input that cannot form an open interest table."""


def scale_column(name: str) -> str:
    return f"{name}_scale"


def readings(
    exchange: str,
    symbol: str,
    label_us: Column,
    lag_us: int,
    values: Mapping[str, Fixed],
    ingested_at: datetime,
) -> PitTable:
    """One contract's readings.

    ``label_us`` are the source's time labels in microseconds since the epoch
    (UTC), ascending; a reading becomes available ``lag_us`` after its label.
    """
    n = len(label_us)
    if set(values) != set(DECIMALS):
        raise OpenInterestError(f"need exactly {DECIMALS}, got {sorted(values)}")
    if {len(v) for v in values.values()} != {n}:
        raise OpenInterestError(f"{symbol}: columns of different lengths")
    if lag_us < 0:
        raise OpenInterestError("a reading cannot be available before its label")
    labels = label_us.cast(pa.int64())
    if labels.null_count or any(values[q].mantissa.null_count for q in QUANTITIES):
        raise OpenInterestError(f"{symbol}: a time label or an open interest is missing")
    if n > 1 and pc.any(pc.less_equal(pc.subtract(labels[1:], labels[:-1]), _ZERO)).as_py():
        raise OpenInterestError(f"{symbol}: time labels are repeated or out of order")
    columns: dict[str, Any] = {
        "exchange": pa.array([exchange] * n, type=pa.string()),
        "symbol": pa.array([symbol] * n, type=pa.string()),
        EVENT_TIME: labels.cast(TIME_TYPE),
        AVAILABLE_AT: pc.add(labels, pa.scalar(lag_us, pa.int64())).cast(TIME_TYPE),
        INGESTED_AT: pa.array([ensure_utc(ingested_at)] * n, type=TIME_TYPE),
    }
    for name in DECIMALS:
        columns[name] = values[name].mantissa
        columns[scale_column(name)] = pa.array([values[name].scale] * n, type=SCALE_TYPE)
    return PitTable(NAME, KEYS, pa.table(columns))


def concat(tables: Sequence[PitTable]) -> PitTable:
    """Tables of one contract (one per day) as one table."""
    return PitTable(NAME, KEYS, pa.concat_tables([t.table for t in tables]))


def problems(t: PitTable) -> list[str]:
    """Rows that are well formed but impossible, one line per kind with a count and an example."""
    table = t.table
    if table.num_rows == 0:
        return []
    found: list[str] = []
    for name in DECIMALS:
        mask = pc.fill_null(pc.less(table.column(name), _ZERO), pa.scalar(False))
        count = int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)
        if count:
            first = table.filter(mask).slice(0, 1).to_pylist()[0]
            found.append(
                f"negative {name}: {count} row(s), first {first['symbol']} {first[EVENT_TIME]}"
            )
    return found
