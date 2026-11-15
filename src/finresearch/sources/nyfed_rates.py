"""New York Fed reference rates: SOFR and the effective federal funds rate (Phase 2.6.3).

The New York Fed publishes each rate once per business day for the business
day before: SOFR at about 8:00 and EFFR at about 9:00, New York time. If an
error is found the same day, the rate may be republished at about 14:30; after
that day a published value is never changed (docs/architecture.md 7.3). The
Markets Data API returns the history of a rate between two dates, as JSON
(``/api/rates/<secured|unsecured>/<rate>/search.json``).

Each response becomes a table of revised series (``data/vintages.py``) named
``rates.nyfed``. A row's ``available_at`` follows from the publication rule:

* a rate followed in the response by the next business day's rate was final
  by 14:30 New York time on that next business day (it was published that
  morning and could only be revised until 14:30), so that is its ``available_at``;
* the latest rate in a response has no next day to go by; it was public when
  we fetched it, so its ``available_at`` is the fetch time. If it was revised
  later that day, the next fetch shows the new value as a new version.

The API also lists the next business day only once the day has passed, so
this rule never dates a value before it was published. Every numeric field
becomes a series: ``SOFR`` is the rate, ``SOFR.p25`` its 25th percentile and so
on (``FIELDS``). EFFR goes back to July 2000; before March 2016 it was computed
by a different method and carries other fields (intraday low and high, standard
deviation). A value the New York Fed did not compute (``NA``, such as the
percentiles on days the rate came from its contingency rules) has no row.
``footnoteId`` points to a footnote on the website and is not kept. Rates
marked revised before 2016 were corrected under rules the
API does not describe, so the 14:30 bound is not documented for them
(``registry/sources/nyfed.reference_rates.toml``).

Terms: storing, copying and distributing are allowed; whoever uses or
distributes the rates must add the New York Fed's notice (docs/architecture.md 7.5).
"""

import itertools
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from finresearch.core.time import ensure_utc, zone
from finresearch.data import fixed, vintages
from finresearch.data.fixed import FixedPointError
from finresearch.data.table import PitTable
from finresearch.data.vintages import Frequency, VintageError
from finresearch.sources import http
from finresearch.sources.base import Adapter, Response, SourceError

SOURCE = "nyfed.reference_rates"
TABLE = "rates.nyfed"
BASE = "https://markets.newyorkfed.org/api/rates"
PUBLISHER_ZONE = "America/New_York"
# The latest time of day at which a rate can still be republished (the revision policy).
FINAL_BY = time(14, 30)
# API field -> series suffix ("" is the rate itself).
FIELDS = {
    "percentRate": "",
    "percentPercentile1": "p1",
    "percentPercentile25": "p25",
    "percentPercentile75": "p75",
    "percentPercentile99": "p99",
    "volumeInBillions": "volume_bn",
    "targetRateFrom": "target_from",
    "targetRateTo": "target_to",
    "intraDayLow": "intraday_low",
    "intraDayHigh": "intraday_high",
    "stdDeviation": "std_dev",
}
TEXT_FIELDS = {"effectiveDate", "type", "revisionIndicator", "footnoteId"}
# How a value that was not computed is written: the percentiles on days the rate was produced
# under the contingency rules (too few transactions reported), for example.
MISSING = {"", "NA"}
REVISION_MARKS = {"", "r"}


def fetch(config: Mapping[str, Any], now: datetime) -> list[Response]:
    """One request per rate: the full history once a week, the recent weeks otherwise."""
    now = ensure_utc(now)
    full = now.weekday() == int(config["full_history_weekday"]) and now.hour < int(
        config["full_history_before_hour"]
    )
    start = (
        date.fromisoformat(str(config["history_start"]))
        if full
        else now.date() - timedelta(days=int(config["lookback_days"]))
    )
    out: list[Response] = []
    for rate, path in dict(config["rates"]).items():
        url = f"{BASE}/{path}/search.json"
        params = {"startDate": start.isoformat(), "endDate": now.date().isoformat()}
        content = http.request("GET", url, params=params)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        out.append(Response(str(rate), f"{url}?{query}", now, content))
    return out


def final_at(day: date) -> datetime:
    """14:30 New York time on ``day``, in UTC."""
    return datetime.combine(day, FINAL_BY, tzinfo=zone(PUBLISHER_ZONE)).astimezone(UTC)


def parse(content: bytes, fetched_at: datetime) -> PitTable:
    """One response -> every numeric field of every rate in it, as versions."""
    fetched_at = ensure_utc(fetched_at)
    try:
        payload = json.loads(content, parse_float=str, parse_int=str)
        rows: list[dict[str, Any]] = list(payload["refRates"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"{SOURCE}: unexpected response shape") from exc
    by_rate: dict[str, dict[date, dict[str, Any]]] = {}
    for row in rows:
        unknown = set(row) - set(FIELDS) - TEXT_FIELDS
        if unknown:
            raise SourceError(f"{SOURCE}: unknown fields {sorted(unknown)}")
        if row.get("revisionIndicator", "") not in REVISION_MARKS:
            raise SourceError(f"{SOURCE}: unknown revision mark {row['revisionIndicator']!r}")
        try:
            rate, day = str(row["type"]), date.fromisoformat(str(row["effectiveDate"]))
        except (KeyError, ValueError) as exc:
            raise SourceError(f"{SOURCE}: a row without a rate name or date") from exc
        days = by_rate.setdefault(rate, {})
        if day in days:
            raise SourceError(f"{SOURCE}: {rate} appears twice for {day}")
        days[day] = row
    tables: list[PitTable] = []
    for rate, days in sorted(by_rate.items()):
        ordered = sorted(days)
        known = {d: final_at(n) for d, n in itertools.pairwise(ordered)}
        if ordered:
            known[ordered[-1]] = fetched_at
        for field, suffix in FIELDS.items():
            have = [d for d in ordered if str(days[d].get(field, "")) not in MISSING]
            if not have:
                continue
            series = f"{rate}.{suffix}" if suffix else rate
            try:
                values = fixed.parse([str(days[d][field]) for d in have])
                tables.append(
                    vintages.versions(
                        TABLE,
                        series,
                        Frequency.DAY,
                        have,
                        values,
                        [known[d] for d in have],
                        fetched_at,
                    )
                )
            except (FixedPointError, VintageError) as exc:
                raise SourceError(f"{SOURCE}: {series}: {exc}") from exc
    return vintages.concat(TABLE, tables)


ADAPTER = Adapter(SOURCE, fetch, parse)
