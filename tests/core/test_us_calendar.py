from datetime import date

import pytest

from finresearch.core.us_calendar import add_business_days, business_day, federal_holidays


def test_holidays_of_2026_as_observed() -> None:
    # Independence Day falls on a Saturday in 2026, so it is observed on Friday 3 July.
    assert sorted(federal_holidays(2026)) == [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 10, 12),
        date(2026, 11, 11),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ]


def test_juneteenth_only_from_2021_and_sunday_holidays_move_to_monday() -> None:
    assert date(2020, 6, 19) not in federal_holidays(2020)
    assert date(2021, 6, 18) in federal_holidays(2021)  # 19 June 2021 was a Saturday
    assert date(2022, 12, 26) in federal_holidays(2022)  # Christmas 2022 was a Sunday
    # New Year's Day 2022 was a Saturday: observed on Friday 31 December 2021.
    assert date(2021, 12, 31) in federal_holidays(2021)
    assert date(2021, 12, 31) not in federal_holidays(2022)


@pytest.mark.parametrize(
    ("start", "n", "closures", "expected"),
    [
        (date(2026, 11, 3), 3, (), date(2026, 11, 6)),  # Tuesday -> Friday
        (date(2026, 11, 10), 3, (), date(2026, 11, 16)),  # Veterans Day on Wednesday
        (date(2025, 12, 23), 3, (date(2025, 12, 24), date(2025, 12, 26)), date(2025, 12, 31)),
    ],
)
def test_business_days(start: date, n: int, closures: tuple[date, ...], expected: date) -> None:
    assert add_business_days(start, n, closures) == expected


def test_a_closure_is_not_a_business_day() -> None:
    assert business_day(date(2025, 1, 8))
    assert not business_day(date(2025, 1, 9), {date(2025, 1, 9)})
    assert not business_day(date(2026, 11, 14))  # a Saturday
