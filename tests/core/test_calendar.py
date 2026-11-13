from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finresearch.core.calendar import (
    CalendarConfigError,
    CalendarCoverageError,
    ContinuousCalendar,
    ExchangeCalendar,
    calendars_from,
    load_calendars,
)
from finresearch.core.time import utc

CALENDARS = load_calendars()
XNYS = CALENDARS["xnys"]
CRYPTO = CALENDARS["crypto"]


def _open_close(day: date) -> tuple[datetime, datetime]:
    s = XNYS.session(day)
    assert s is not None
    return s.open, s.close


def test_configured_calendars() -> None:
    assert isinstance(XNYS, ExchangeCalendar)
    assert isinstance(CRYPTO, ContinuousCalendar)


def test_new_york_hours_follow_daylight_saving() -> None:
    assert _open_close(date(2026, 11, 13)) == (
        utc(2026, 11, 13, 14, 30),
        utc(2026, 11, 13, 21),
    )  # winter
    assert _open_close(date(2026, 7, 1)) == (utc(2026, 7, 1, 13, 30), utc(2026, 7, 1, 20))  # summer
    # clocks go forward on Sunday 2026-03-08 and back on Sunday 2026-11-01
    assert _open_close(date(2026, 3, 6))[0] == utc(2026, 3, 6, 14, 30)
    assert _open_close(date(2026, 3, 9))[0] == utc(2026, 3, 9, 13, 30)
    assert _open_close(date(2026, 10, 30))[0] == utc(2026, 10, 30, 13, 30)
    assert _open_close(date(2026, 11, 2))[0] == utc(2026, 11, 2, 14, 30)


def test_holidays_weekends_and_early_closes() -> None:
    assert XNYS.session(date(2026, 11, 26)) is None  # Thanksgiving
    assert XNYS.session(date(2026, 11, 14)) is None  # Saturday
    after_thanksgiving = XNYS.session(date(2026, 11, 27))
    assert after_thanksgiving is not None
    assert after_thanksgiving.early_close
    assert after_thanksgiving.close == utc(2026, 11, 27, 18)  # 13:00 New York, winter
    day_before_july_4 = XNYS.session(date(2028, 7, 3))
    assert day_before_july_4 is not None
    assert day_before_july_4.close == utc(2028, 7, 3, 17)  # 13:00 New York, summer


def test_trading_day_count() -> None:
    # 2026 starts on a Thursday: 52 full weeks + 1 Thursday = 261 weekdays, minus 10 holidays
    assert len(XNYS.sessions(date(2026, 1, 1), date(2026, 12, 31))) == 251
    assert [s.day.day for s in XNYS.sessions(date(2026, 11, 23), date(2026, 11, 29))] == [
        23,
        24,
        25,
        27,
    ]


def test_open_is_half_open() -> None:
    assert not XNYS.is_open(utc(2026, 11, 13, 14, 29, 59))
    assert XNYS.is_open(utc(2026, 11, 13, 14, 30))
    assert XNYS.is_open(utc(2026, 11, 13, 20, 59, 59))
    assert not XNYS.is_open(utc(2026, 11, 13, 21))
    assert XNYS.session_at(utc(2026, 11, 13, 22)) is None


def test_last_closed_and_next_session() -> None:
    assert XNYS.last_closed(utc(2026, 11, 14, 12)).day == date(2026, 11, 13)  # Saturday
    assert XNYS.last_closed(utc(2026, 11, 13, 21)).day == date(2026, 11, 13)  # exactly at the close
    assert XNYS.last_closed(utc(2026, 11, 13, 20, 59)).day == date(2026, 11, 12)  # still trading
    assert XNYS.last_closed(utc(2026, 11, 27, 19)).day == date(
        2026, 11, 27
    )  # after the early close
    assert XNYS.last_closed(utc(2026, 11, 26, 22)).day == date(2026, 11, 25)  # Thanksgiving
    assert XNYS.next_session(utc(2026, 11, 25, 21, 30)).day == date(2026, 11, 27)
    assert XNYS.next_session(utc(2026, 11, 13, 14, 30)).day == date(2026, 11, 13)
    assert XNYS.next_session(utc(2026, 11, 13, 14, 31)).day == date(2026, 11, 16)


def test_outside_coverage_raises_instead_of_guessing() -> None:
    with pytest.raises(CalendarCoverageError):
        XNYS.session(date(2025, 12, 31))
    with pytest.raises(CalendarCoverageError):
        XNYS.session(date(2029, 1, 2))
    with pytest.raises(CalendarCoverageError):  # Jan 1 is a holiday, the walk back leaves coverage
        XNYS.last_closed(utc(2026, 1, 1, 12))


def test_continuous_calendar() -> None:
    s = CRYPTO.session(date(2026, 11, 13))
    assert s is not None
    assert (s.open, s.close) == (utc(2026, 11, 13), utc(2026, 11, 14))
    assert CRYPTO.is_open(utc(2026, 11, 14, 3))
    assert CRYPTO.last_closed(utc(2026, 11, 13)).day == date(2026, 11, 12)
    assert CRYPTO.last_closed(utc(2026, 11, 13, 5)).day == date(2026, 11, 12)
    assert CRYPTO.next_session(utc(2026, 11, 13)).day == date(2026, 11, 13)
    assert CRYPTO.next_session(utc(2026, 11, 13, 0, 0, 1)).day == date(2026, 11, 14)
    assert len(CRYPTO.sessions(date(2028, 2, 1), date(2028, 2, 29))) == 29  # leap year


def _xnys_spec(**overrides: object) -> dict[str, Any]:
    spec: dict[str, object] = {
        "kind": "exchange",
        "timezone": "America/New_York",
        "open": "09:30",
        "close": "16:00",
        "early_close": "13:00",
        "weekdays": [0, 1, 2, 3, 4],
        "coverage": ["2026-01-01", "2026-12-31"],
        "holidays": ["2026-11-26"],
        "early_closes": ["2026-11-27"],
    }
    spec.update(overrides)
    return {"test": spec}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"holidays": ["2026-11-28"]}, "not a trading weekday"),  # a Saturday
        ({"holidays": ["2027-01-01"]}, "outside coverage"),
        ({"early_closes": ["2026-11-26"]}, "holiday and early close"),
        ({"open": "16:00"}, "open < early_close < close"),
        ({"timezone": "Mars/Olympus_Mons"}, "unknown time zone"),
        ({"kind": "lunar"}, "unknown calendar kind"),
    ],
)
def test_inconsistent_configuration_is_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(CalendarConfigError, match=message):
        calendars_from(_xnys_spec(**overrides))


# hypothesis takes naive bounds and attaches the time zone itself
covered = st.datetimes(
    min_value=datetime(2026, 1, 6),  # noqa: DTZ001
    max_value=datetime(2028, 12, 22),  # noqa: DTZ001
    timezones=st.just(UTC),
)


@given(covered)
def test_calendar_queries_agree(t: datetime) -> None:
    current = XNYS.session_at(t)
    assert XNYS.is_open(t) == (current is not None)
    assert XNYS.last_closed(t).close <= t
    assert XNYS.next_session(t).open >= t
    if current is not None:
        assert current.open <= t < current.close


def test_sessions_never_overlap() -> None:
    sessions = XNYS.sessions(date(2026, 1, 1), date(2028, 12, 31))
    assert len(sessions) == 251 * 3  # 2027 and 2028 also come to 251 (hand count in the PR)
    for earlier, later in pairwise(sessions):
        assert earlier.close < later.open
        assert later.day - earlier.day >= timedelta(days=1)
