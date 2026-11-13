from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finresearch.core.time import (
    EPOCH,
    Interval,
    NaiveDatetimeError,
    ensure_utc,
    floor_time,
    format_utc,
    from_epoch_ms,
    from_epoch_us,
    month_start,
    parse_utc,
    to_epoch_ms,
    to_epoch_us,
    utc,
    zone,
)

Y2K_SECONDS = 946_684_800  # 2000-01-01T00:00:00Z, the textbook anchor: 10957 days * 86400 s

# hypothesis takes naive bounds and attaches the time zone itself
instants = st.datetimes(
    min_value=datetime(1900, 1, 1),  # noqa: DTZ001
    max_value=datetime(2200, 1, 1),  # noqa: DTZ001
    timezones=st.just(UTC),
)
steps = st.timedeltas(min_value=timedelta(microseconds=1), max_value=timedelta(days=7))


def test_epoch_anchors() -> None:
    assert to_epoch_us(EPOCH) == 0
    assert to_epoch_us(utc(2000, 1, 1)) == Y2K_SECONDS * 1_000_000
    assert to_epoch_ms(utc(2000, 1, 1)) == Y2K_SECONDS * 1_000
    assert from_epoch_ms(Y2K_SECONDS * 1_000 + 123) == utc(2000, 1, 1, microsecond=123_000)


def test_coarser_units_floor_toward_the_past() -> None:
    assert to_epoch_ms(utc(2000, 1, 1, microsecond=999)) == Y2K_SECONDS * 1_000
    one_us_before_epoch = utc(1969, 12, 31, 23, 59, 59, 999_999)
    assert to_epoch_us(one_us_before_epoch) == -1
    assert to_epoch_ms(one_us_before_epoch) == -1  # not 0: rounding must never move into the future


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_utc(datetime(2026, 11, 13, 3, 15))  # noqa: DTZ001 - the point of the test
    with pytest.raises(NaiveDatetimeError):
        to_epoch_us(datetime(2026, 11, 13))  # noqa: DTZ001
    with pytest.raises(NaiveDatetimeError):
        parse_utc("2026-11-13T03:15:13")


def test_other_offsets_are_converted_to_utc() -> None:
    kuala_lumpur = timezone(timedelta(hours=8))
    t = ensure_utc(datetime(2026, 11, 13, 11, 15, tzinfo=kuala_lumpur))
    assert t == utc(2026, 11, 13, 3, 15)
    assert t.tzinfo is UTC
    assert parse_utc("2026-11-13T11:15:13+08:00") == utc(2026, 11, 13, 3, 15, 13)
    assert parse_utc("2026-11-13T03:15:13Z") == utc(2026, 11, 13, 3, 15, 13)


def test_canonical_text_form() -> None:
    assert format_utc(utc(2026, 11, 13, 3, 15, 13)) == "2026-11-13T03:15:13.000000Z"
    assert format_utc(utc(2026, 11, 13, 3, 15, 13, 42)) == "2026-11-13T03:15:13.000042Z"


def test_floor_time() -> None:
    t = utc(2026, 11, 13, 3, 15, 13, 500)
    assert floor_time(t, timedelta(minutes=1)) == utc(2026, 11, 13, 3, 15)
    assert floor_time(t, timedelta(hours=1)) == utc(2026, 11, 13, 3)
    assert floor_time(t, timedelta(days=1)) == utc(2026, 11, 13)
    assert floor_time(utc(1969, 12, 31, 23, 59, 30), timedelta(minutes=1)) == utc(
        1969, 12, 31, 23, 59
    )
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="positive"):
            floor_time(t, bad)


def test_zone_comes_from_tzdata_and_knows_daylight_saving() -> None:
    new_york = zone("America/New_York")
    assert new_york.key == "America/New_York"
    assert utc(2026, 7, 1, 12).astimezone(new_york).utcoffset() == timedelta(hours=-4)
    assert utc(2026, 12, 1, 12).astimezone(new_york).utcoffset() == timedelta(hours=-5)
    with pytest.raises(FileNotFoundError):
        zone("Mars/Olympus_Mons")


def test_interval_is_half_open() -> None:
    i = Interval(utc(2026, 11, 13, 14, 30), utc(2026, 11, 13, 21))
    assert i.duration == timedelta(hours=6, minutes=30)
    assert i.contains(utc(2026, 11, 13, 14, 30))
    assert not i.contains(utc(2026, 11, 13, 21))
    assert not i.overlaps(
        Interval(utc(2026, 11, 13, 21), utc(2026, 11, 13, 22))
    )  # touching is not overlapping
    assert i.overlaps(Interval(utc(2026, 11, 13, 20), utc(2026, 11, 13, 22)))


def test_interval_rejects_empty_and_naive() -> None:
    with pytest.raises(ValueError, match="start < end"):
        Interval(utc(2026, 1, 1), utc(2026, 1, 1))
    with pytest.raises(NaiveDatetimeError):
        Interval(datetime(2026, 1, 1), utc(2026, 1, 2))  # noqa: DTZ001


@given(instants)
def test_epoch_round_trips(t: datetime) -> None:
    assert from_epoch_us(to_epoch_us(t)) == t
    assert parse_utc(format_utc(t)) == t
    ms = to_epoch_ms(t)
    assert ms * 1_000 <= to_epoch_us(t) < (ms + 1) * 1_000


@given(instants, steps)
def test_floor_time_properties(t: datetime, step: timedelta) -> None:
    f = floor_time(t, step)
    assert f <= t < f + step
    assert floor_time(f, step) == f


def test_month_start() -> None:
    assert month_start(date(2026, 11, 13)) == date(2026, 11, 1)
    assert month_start(date(2026, 11, 13), 2) == date(2027, 1, 1)
    assert month_start(date(2026, 1, 31), -1) == date(2025, 12, 1)
    assert month_start(date(2000, 3, 1), -26) == date(1998, 1, 1)


@given(st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)), st.integers(-600, 600))
def test_month_start_round_trips(d: date, k: int) -> None:
    assert month_start(month_start(d, k), -k) == month_start(d)
