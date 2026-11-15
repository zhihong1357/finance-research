import json
from datetime import date, datetime
from decimal import Decimal

import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.data import vintages
from finresearch.data.table import PitTable, versions_as_of
from finresearch.sources.base import SourceError
from finresearch.sources.nyfed_rates import ADAPTER, BASE, TABLE, final_at, parse
from tests.sources.conftest import FakeServer

CONFIG = {
    "rates": {"SOFR": "secured/sofr", "EFFR": "unsecured/effr"},
    "lookback_days": 30,
    "history_start": "2000-01-01",
    "full_history_weekday": 6,
    "full_history_before_hour": 8,
}
# Made-up numbers in the API's format.
MON = {
    "effectiveDate": "2026-11-09",
    "type": "SOFR",
    "percentRate": 4.11,
    "percentPercentile1": 4.05,
    "percentPercentile25": "NA",
    "volumeInBillions": 2000,
    "revisionIndicator": "",
}
TUE = {**MON, "effectiveDate": "2026-11-10", "percentRate": 4.12, "percentPercentile25": 4.1}
THU = {**MON, "effectiveDate": "2026-11-12", "percentRate": 4.15, "footnoteId": 2}


def _body(rows: list[dict[str, object]]) -> bytes:
    return json.dumps({"refRates": rows}).encode()


def _value(t: PitTable, series: str, day: date) -> list[tuple[datetime, Decimal]]:
    return vintages.revisions(t, series, day)


def test_recent_weeks_usually_and_the_whole_history_on_sunday_night(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(200, content=_body([]))
    tuesday = ADAPTER.fetch(CONFIG, utc(2026, 11, 17, 8, 17))
    assert [r.name for r in tuesday] == ["SOFR", "EFFR"]
    assert [str(q.url) for q in server.requests] == [
        f"{BASE}/secured/sofr/search.json?startDate=2026-10-18&endDate=2026-11-17",
        f"{BASE}/unsecured/effr/search.json?startDate=2026-10-18&endDate=2026-11-17",
    ]
    ADAPTER.fetch(CONFIG, utc(2026, 11, 15, 0, 17))  # Sunday, the first run of the day
    ADAPTER.fetch(CONFIG, utc(2026, 11, 15, 8, 17))  # Sunday, the second run
    assert [str(q.url).split("?")[1] for q in server.requests[2:]] == [
        "startDate=2000-01-01&endDate=2026-11-15",
        "startDate=2000-01-01&endDate=2026-11-15",
        "startDate=2026-10-16&endDate=2026-11-15",
        "startDate=2026-10-16&endDate=2026-11-15",
    ]


def test_final_at_is_half_past_two_in_new_york() -> None:
    assert final_at(date(2026, 11, 12)) == utc(2026, 11, 12, 19, 30)  # winter: UTC-5
    assert final_at(date(2026, 7, 2)) == utc(2026, 7, 2, 18, 30)  # summer: UTC-4


def test_a_rate_is_final_on_the_next_business_day_and_the_latest_when_fetched() -> None:
    # Wednesday 11 November was a holiday: Tuesday's rate was published on Thursday.
    fetched = utc(2026, 11, 13, 13, 5)
    t = parse(_body([THU, TUE, MON]), fetched)
    assert t.name == TABLE
    assert _value(t, "SOFR", date(2026, 11, 9)) == [(utc(2026, 11, 10, 19, 30), Decimal("4.11"))]
    assert _value(t, "SOFR", date(2026, 11, 10)) == [(utc(2026, 11, 12, 19, 30), Decimal("4.12"))]
    assert _value(t, "SOFR", date(2026, 11, 12)) == [(fetched, Decimal("4.15"))]
    assert _value(t, "SOFR.volume_bn", date(2026, 11, 9)) == [
        (utc(2026, 11, 10, 19, 30), Decimal(2000))
    ]
    # NA is not a value: Monday has no 25th percentile.
    assert [d for d, _ in _value(t, "SOFR.p25", date(2026, 11, 9))] == []
    assert len(_value(t, "SOFR.p25", date(2026, 11, 10))) == 1
    assert set(t.table.column("series").to_pylist()) == {
        "SOFR",
        "SOFR.p1",
        "SOFR.p25",
        "SOFR.volume_bn",
    }


def test_a_same_day_revision_seen_by_two_fetches_becomes_two_versions() -> None:
    # 08:05 New York time: Thursday's rate as first published. The next day the API
    # shows it revised, and Friday's rate after it.
    morning = parse(_body([TUE, THU]), utc(2026, 11, 13, 13, 5))
    fri = {**MON, "effectiveDate": "2026-11-13", "percentRate": 4.2}
    revised = {**THU, "percentRate": 4.16, "revisionIndicator": "r"}
    next_day = parse(_body([TUE, revised, fri]), utc(2026, 11, 16, 13, 5))
    t = vintages.compact(vintages.merge(TABLE, [morning, next_day]))
    assert _value(t, "SOFR", date(2026, 11, 12)) == [
        (utc(2026, 11, 13, 13, 5), Decimal("4.15")),
        (utc(2026, 11, 13, 19, 30), Decimal("4.16")),
    ]
    at_noon = versions_as_of(t, utc(2026, 11, 13, 17)).to_pylist()
    assert [(r["series"], r["value"]) for r in at_noon if r["series"] == "SOFR"] == [
        ("SOFR", 412),
        ("SOFR", 415),
    ]
    # Tuesday's rate came twice with one value: one version, from the first fetch.
    tue = [r for r in t.table.to_pylist() if r["series"] == "SOFR" and r["event_time"].day == 10]
    assert [r["ingested_at"] for r in tue] == [utc(2026, 11, 13, 13, 5)]


def test_fetches_that_disagree_about_one_version_are_refused() -> None:
    a = parse(_body([TUE, THU]), utc(2026, 11, 14))
    b = parse(_body([{**TUE, "percentRate": 4.13}, THU]), utc(2026, 11, 15))
    with pytest.raises(vintages.VintageError, match="disagree"):
        vintages.merge(TABLE, [a, b])


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{**MON, "newField": 1}], "unknown fields"),
        ([{**MON, "revisionIndicator": "x"}], "revision mark"),
        ([MON, MON], "appears twice"),
        ([{**MON, "percentRate": "high"}], "SOFR"),
        ([{k: v for k, v in MON.items() if k != "effectiveDate"}], "without a rate name or date"),
    ],
)
def test_unexpected_content_is_refused(rows: list[dict[str, object]], message: str) -> None:
    with pytest.raises(SourceError, match=message):
        parse(_body(rows), utc(2026, 11, 14))


def test_a_response_of_another_shape_is_refused() -> None:
    with pytest.raises(SourceError, match="unexpected response shape"):
        parse(b'{"rates": []}', utc(2026, 11, 14))
    assert parse(_body([]), utc(2026, 11, 14)).num_rows == 0
