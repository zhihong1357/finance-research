from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.data import vintages
from finresearch.sources.base import SourceError
from finresearch.sources.cftc_cot import (
    ADAPTER,
    COLUMNS,
    FIELDS,
    TABLE,
    URL,
    calendar,
    check_not_late,
    parse,
)
from tests.sources.conftest import FakeServer

# The CFTC's official 2026 release dates (cftc.gov, release schedule page, read 2026-11-15).
RELEASES_2026 = {
    1: (5, 9, 16, 23, 30),
    2: (6, 13, 20, 27),
    3: (6, 13, 20, 27),
    4: (3, 10, 17, 24),
    5: (1, 8, 15, 22, 29),
    6: (5, 12, 22, 26),
    7: (6, 10, 17, 24, 31),
    8: (7, 14, 21, 28),
    9: (4, 11, 18, 25),
    10: (2, 9, 16, 23, 30),
    11: (6, 16, 20, 30),
    12: (4, 11, 18, 28),
}


def test_the_rule_gives_every_official_2026_release_date() -> None:
    cal = calendar()
    report = date(2025, 12, 30)
    expected = sorted(date(2026, m, d) for m, days in RELEASES_2026.items() for d in days)
    got = []
    while report <= date(2026, 12, 22):
        got.append(cal.release(report).date())
        report = cal.next_report(report)
    assert got == expected
    # 15:30 in New York: 20:30 UTC in winter, 19:30 in summer.
    assert cal.release(date(2026, 11, 3)) == utc(2026, 11, 6, 20, 30)
    assert cal.release(date(2026, 7, 7)) == utc(2026, 7, 10, 19, 30)


@pytest.mark.parametrize(
    ("report", "released"),
    [
        (date(2024, 11, 26), utc(2024, 12, 2, 20, 30)),  # Thanksgiving: released Monday
        (date(2025, 12, 23), utc(2025, 12, 31, 20, 30)),  # offices closed 24 to 26 December
        (date(2025, 10, 7), utc(2025, 12, 29, 20, 30)),  # the 2025 shutdown
        (date(2019, 1, 15), utc(2019, 3, 8, 20, 30)),  # the 2018-19 shutdown
    ],
)
def test_holidays_closures_and_shutdowns(report: date, released: datetime) -> None:
    assert calendar().release(report) == released


def test_the_next_report_is_the_tuesday_of_the_following_week() -> None:
    assert calendar().next_report(date(2026, 11, 3)) == date(2026, 11, 10)
    # After a Monday report (Tuesday 25 December 2018 was a holiday), the week after.
    assert calendar().next_report(date(2018, 12, 24)) == date(2019, 1, 1)


def test_a_late_report_is_noticed() -> None:
    # The 10 November report is due 16 November 20:30 UTC; a day's grace after that.
    check_not_late([date(2026, 11, 3)], utc(2026, 11, 17, 20), calendar())
    with pytest.raises(SourceError, match="was due by"):
        check_not_late([date(2026, 11, 3)], utc(2026, 11, 17, 21), calendar())
    with pytest.raises(SourceError, match="no report"):
        check_not_late([], utc(2026, 11, 17), calendar())


def _csv(rows: list[tuple[str, str, dict[str, str]]]) -> bytes:
    """Made-up rows in the API's CSV layout: contract code, report date, some fields."""
    lines = [",".join(f'"{c}"' for c in COLUMNS)]
    for code, day, values in rows:
        cells = [code, f"{day}T00:00:00.000"] + [values.get(f, "") for f in FIELDS]
        lines.append(",".join(f'"{c}"' for c in cells))
    return ("\n".join(lines) + "\n").encode()


def test_each_field_of_each_contract_becomes_a_series() -> None:
    content = _csv(
        [
            (
                "133741",
                "2026-10-27",
                {"open_interest_all": "31000", "conc_net_le_4_tdr_long_all": "18.2"},
            ),
            ("133741", "2026-11-03", {"open_interest_all": "31500"}),
        ]
    )
    t = parse(content, utc(2026, 11, 15))
    assert t.name == TABLE
    assert vintages.revisions(t, "133741.open_interest_all", date(2026, 10, 27)) == [
        (utc(2026, 10, 30, 19, 30), Decimal(31000))
    ]
    assert vintages.revisions(t, "133741.conc_net_le_4_tdr_long_all", date(2026, 10, 27)) == [
        (utc(2026, 10, 30, 19, 30), Decimal("18.2"))
    ]
    assert t.num_rows == 3  # empty cells are not values


def test_a_report_seen_before_the_rules_time_is_dated_when_seen() -> None:
    seen = utc(2026, 11, 6, 20, 0)  # half an hour before the rule's 20:30
    t = parse(_csv([("133741", "2026-11-03", {"open_interest_all": "1"})]), seen)
    assert vintages.revisions(t, "133741.open_interest_all", date(2026, 11, 3))[0][0] == seen


def test_text_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(SourceError, match="open_interest_all"):
        parse(_csv([("133741", "2026-11-03", {"open_interest_all": "many"})]), utc(2026, 11, 15))


def test_fetch_pages_through_long_answers_and_checks_lateness(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finresearch.sources import cftc_cot

    monkeypatch.setattr(cftc_cot, "_PAGE", 2)
    pages = [
        _csv([("1", "2026-10-27", {}), ("2", "2026-10-27", {})]),
        _csv([("1", "2026-11-03", {})]),
    ]
    server.handler = lambda r: httpx2.Response(
        200, content=pages[int(r.url.params["$offset"]) // 2]
    )
    config = {
        "added": "2026-11-16T00:00:00Z",
        "history_start": "2006-01-01",
        "lookback_days": 60,
        "backfill_days": 7,
        "full_history_months": [1, 4, 7, 10],
        "full_history_first_days": 7,
    }
    now = utc(2026, 11, 16, 6)
    responses = ADAPTER.fetch(config, now)
    assert [r.name for r in responses] == ["tff-full-0", "tff-full-1"]
    assert all(r.url.startswith(URL) for r in responses)
    assert server.requests[0].url.params["$where"].endswith("'2006-01-01T00:00:00'")
    # Long after it was added: the last 60 days only.
    server.requests.clear()
    ADAPTER.fetch({**config, "added": "2026-01-01T00:00:00Z"}, now)
    assert server.requests[0].url.params["$where"].endswith("'2026-09-17T00:00:00'")
    # The next report is due 16 November 20:30 UTC; two days later it is late.
    with pytest.raises(SourceError, match="was due by"):
        ADAPTER.fetch({**config, "added": "2026-01-01T00:00:00Z"}, now + timedelta(days=2))
