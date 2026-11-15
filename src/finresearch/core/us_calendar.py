"""U.S. federal holidays, for the release calendars of U.S. agencies (Phase 2.6.5).

The holidays of 5 U.S.C. 6103: New Year's Day, Birthday of Martin Luther King
Jr. (third Monday of January), Washington's Birthday (third Monday of
February), Memorial Day (last Monday of May), Juneteenth (June 19, from 2021),
Independence Day, Labor Day (first Monday of September), Columbus Day (second
Monday of October), Veterans Day, Thanksgiving (fourth Thursday of November)
and Christmas. A holiday on a Saturday is observed on the Friday before, one on
a Sunday on the Monday after.

Offices also close on days that are not holidays (executive orders, Inauguration
Day in Washington, weather); callers pass those in from their configuration.
"""

from collections.abc import Collection
from datetime import date, timedelta

_SATURDAY, _SUNDAY = 5, 6
_MONDAY, _THURSDAY = 0, 3
_JUNETEENTH_FROM = 2021
_WEEK = 7


def _nth(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` of a month; ``n = -1`` is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % _WEEK + _WEEK * (n - 1))
    nxt = date(year + month // 12, month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % _WEEK)


def _observed(d: date) -> date:
    if d.weekday() == _SATURDAY:
        return d - timedelta(days=1)
    if d.weekday() == _SUNDAY:
        return d + timedelta(days=1)
    return d


def federal_holidays(year: int) -> set[date]:
    """The days federal offices are closed for a holiday in ``year`` (as observed)."""
    fixed = [date(year, 1, 1), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    if year >= _JUNETEENTH_FROM:
        fixed.append(date(year, 6, 19))
    days = {_observed(d) for d in fixed}
    # New Year's Day of next year falls on this year's December 31 when it is a Saturday.
    days.add(_observed(date(year + 1, 1, 1)))
    days |= {
        _nth(year, 1, _MONDAY, 3),
        _nth(year, 2, _MONDAY, 3),
        _nth(year, 5, _MONDAY, -1),
        _nth(year, 9, _MONDAY, 1),
        _nth(year, 10, _MONDAY, 2),
        _nth(year, 11, _THURSDAY, 4),
    }
    return {d for d in days if d.year == year}


def business_day(d: date, closures: Collection[date] = ()) -> bool:
    return d.weekday() < _SATURDAY and d not in federal_holidays(d.year) and d not in closures


def add_business_days(d: date, n: int, closures: Collection[date] = ()) -> date:
    """The ``n``-th business day after ``d`` (``d`` itself not counted)."""
    out = d
    while n > 0:
        out += timedelta(days=1)
        if business_day(out, closures):
            n -= 1
    return out
