# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Funding rates of research universe U1: one table shape for every exchange (Phase 2.5.6).

A perpetual contract has no expiry; what ties its price to the spot price is
the *funding payment*: at each settlement, longs pay shorts the funding rate
times their position's value (or receive it, when the rate is negative). A row
is one settlement of one contract:

* keys ``exchange`` and ``symbol`` (the archive's folder name, as in the bars);
* ``event_time`` is the settlement instant as the exchange recorded it, which
  is on the hour or a few milliseconds after it;
* ``available_at`` is the same instant: the rate that is charged was public
  before the settlement, so the settlement instant is the conservative choice;
* ``ingested_at`` is the moment the archive published the file;
* ``funding_rate`` is the rate charged, an exact fixed-point mantissa
  (``data/fixed.py``) with its scale in ``funding_rate_scale``: 0.0001 is one
  basis point of the position's value;
* ``interval_hours`` is the contract's funding interval at that settlement,
  as the exchange states it (it changes over a contract's life).
"""

from collections.abc import Sequence
from datetime import datetime

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.time import ensure_utc
from finresearch.data.fixed import SCALE_TYPE, Column, Fixed
from finresearch.data.table import AVAILABLE_AT, EVENT_TIME, INGESTED_AT, TIME_TYPE, PitTable

NAME = "u1.funding"
KEYS = ("exchange", "symbol")
RATE = "funding_rate"
RATE_SCALE = "funding_rate_scale"
INTERVAL = "interval_hours"
INTERVAL_TYPE = pa.int8()
_ZERO = pa.scalar(0, pa.int64())


class FundingError(ValueError):
    """Input that cannot form a funding table."""


def funding(
    exchange: str,
    symbol: str,
    time_us: Column,
    rate: Fixed,
    interval_hours: Column,
    ingested_at: datetime,
) -> PitTable:
    """One contract's settlements; ``time_us`` are microseconds since the epoch (UTC), ascending."""
    n = len(time_us)
    if not (len(rate) == len(interval_hours) == n):
        raise FundingError(f"{symbol}: columns of different lengths")
    times = time_us.cast(pa.int64())
    if times.null_count or interval_hours.null_count:
        raise FundingError(f"{symbol}: missing settlement times or intervals")
    if n > 1 and pc.any(pc.less_equal(pc.subtract(times[1:], times[:-1]), _ZERO)).as_py():
        raise FundingError(f"{symbol}: settlements are repeated or out of order")
    if n and pc.any(pc.less_equal(interval_hours, _ZERO)).as_py():
        raise FundingError(f"{symbol}: a funding interval is not positive")
    try:
        intervals = interval_hours.cast(INTERVAL_TYPE)
    except pa.ArrowInvalid as exc:
        raise FundingError(f"{symbol}: a funding interval is too long: {exc}") from exc
    event = times.cast(TIME_TYPE)
    return PitTable(
        NAME,
        KEYS,
        pa.table(
            {
                "exchange": pa.array([exchange] * n, type=pa.string()),
                "symbol": pa.array([symbol] * n, type=pa.string()),
                EVENT_TIME: event,
                AVAILABLE_AT: event,
                INGESTED_AT: pa.array([ensure_utc(ingested_at)] * n, type=TIME_TYPE),
                RATE: rate.mantissa,
                RATE_SCALE: pa.array([rate.scale] * n, type=SCALE_TYPE),
                INTERVAL: intervals,
            }
        ),
    )


def concat(tables: Sequence[PitTable]) -> PitTable:
    """Funding tables of one contract (for example one per month) as one table."""
    return PitTable(NAME, KEYS, pa.concat_tables([t.table for t in tables]))
