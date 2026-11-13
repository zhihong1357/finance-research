"""Trading calendars.

A *session* is one trading day as the venue labels it, with its open and close
as UTC instants. Two kinds of venue:

* ``ContinuousCalendar`` - trades around the clock (crypto). Sessions are UTC
  calendar days, used only to group data by day.
* ``ExchangeCalendar`` - fixed local hours on weekdays, minus holidays, with
  some early closes. Local hours are converted with the pinned ``tzdata``, so
  daylight saving is handled exactly (New York opens at 14:30 UTC in winter
  and 13:30 UTC in summer).

An exchange calendar only knows the holidays inside its ``coverage``. Any
question about a date outside it raises ``CalendarCoverageError``: a calendar
that guessed would silently put trades on days the market was shut.

Parameters live in ``config/calendars.toml``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from finresearch.core.config import load_toml
from finresearch.core.time import Interval, ensure_utc, zone

_ONE_DAY = timedelta(days=1)


class CalendarCoverageError(LookupError):
    """The calendar has no holiday data for the requested date."""


class CalendarConfigError(ValueError):
    """The calendar configuration is inconsistent."""


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    interval: Interval
    early_close: bool = False

    @property
    def open(self) -> datetime:
        return self.interval.start

    @property
    def close(self) -> datetime:
        return self.interval.end


class Calendar(Protocol):
    @property
    def name(self) -> str: ...

    def session(self, day: date) -> Session | None:
        """The session labelled ``day``, or None if the venue does not trade that day."""
        ...

    def session_at(self, t: datetime) -> Session | None:
        """The session in progress at instant ``t``, or None if the venue is closed."""
        ...

    def is_open(self, t: datetime) -> bool: ...

    def sessions(self, first: date, last: date) -> list[Session]:
        """All sessions labelled ``first`` .. ``last`` inclusive."""
        ...

    def last_closed(self, t: datetime) -> Session:
        """The most recent session whose close is at or before ``t``."""
        ...

    def next_session(self, t: datetime) -> Session:
        """The first session whose open is at or after ``t``."""
        ...


def _days(first: date, last: date) -> Iterable[date]:
    d = first
    while d <= last:
        yield d
        d += _ONE_DAY


@dataclass(frozen=True, slots=True)
class ContinuousCalendar:
    name: str

    def session(self, day: date) -> Session:
        start = datetime.combine(day, time(0), tzinfo=UTC)
        return Session(day, Interval(start, start + _ONE_DAY))

    def session_at(self, t: datetime) -> Session:
        return self.session(ensure_utc(t).date())

    def is_open(self, t: datetime) -> bool:
        ensure_utc(t)
        return True

    def sessions(self, first: date, last: date) -> list[Session]:
        return [self.session(d) for d in _days(first, last)]

    def last_closed(self, t: datetime) -> Session:
        return self.session(ensure_utc(t).date() - _ONE_DAY)

    def next_session(self, t: datetime) -> Session:
        t = ensure_utc(t)
        today = self.session(t.date())
        return today if today.open == t else self.session(t.date() + _ONE_DAY)


@dataclass(frozen=True, slots=True)
class ExchangeCalendar:
    name: str
    timezone: str
    open_local: time
    close_local: time
    early_close_local: time
    weekdays: frozenset[int]
    holidays: frozenset[date]
    early_closes: frozenset[date]
    first: date
    last: date

    def __post_init__(self) -> None:
        if not self.open_local < self.early_close_local < self.close_local:
            raise CalendarConfigError(f"{self.name}: need open < early_close < close")
        if not self.first <= self.last:
            raise CalendarConfigError(f"{self.name}: coverage must start before it ends")
        for kind, days in (("holiday", self.holidays), ("early close", self.early_closes)):
            for d in sorted(days):
                if not self.first <= d <= self.last:
                    raise CalendarConfigError(f"{self.name}: {kind} {d} is outside coverage")
                if d.weekday() not in self.weekdays:
                    raise CalendarConfigError(f"{self.name}: {kind} {d} is not a trading weekday")
        if both := self.holidays & self.early_closes:
            raise CalendarConfigError(
                f"{self.name}: {sorted(both)} listed as holiday and early close"
            )
        try:
            zone(self.timezone)
        except (FileNotFoundError, ValueError) as exc:
            raise CalendarConfigError(f"{self.name}: unknown time zone {self.timezone!r}") from exc

    def _check(self, day: date) -> None:
        if not self.first <= day <= self.last:
            raise CalendarCoverageError(
                f"{self.name}: no holiday data for {day} (coverage {self.first} .. {self.last})"
            )

    def _local_date(self, t: datetime) -> date:
        return ensure_utc(t).astimezone(zone(self.timezone)).date()

    def session(self, day: date) -> Session | None:
        self._check(day)
        if day.weekday() not in self.weekdays or day in self.holidays:
            return None
        tz = zone(self.timezone)
        early = day in self.early_closes
        start = datetime.combine(day, self.open_local, tzinfo=tz)
        end = datetime.combine(
            day, self.early_close_local if early else self.close_local, tzinfo=tz
        )
        return Session(day, Interval(start, end), early_close=early)

    def session_at(self, t: datetime) -> Session | None:
        s = self.session(self._local_date(t))
        return s if s is not None and s.interval.contains(t) else None

    def is_open(self, t: datetime) -> bool:
        return self.session_at(t) is not None

    def sessions(self, first: date, last: date) -> list[Session]:
        return [s for d in _days(first, last) if (s := self.session(d)) is not None]

    def last_closed(self, t: datetime) -> Session:
        t = ensure_utc(t)
        d = self._local_date(t)
        while True:
            s = self.session(d)  # raises once it walks off the start of coverage
            if s is not None and s.close <= t:
                return s
            d -= _ONE_DAY

    def next_session(self, t: datetime) -> Session:
        t = ensure_utc(t)
        d = self._local_date(t)
        while True:
            s = self.session(d)  # raises once it walks off the end of coverage
            if s is not None and s.open >= t:
                return s
            d += _ONE_DAY


def _dates(values: list[Any]) -> frozenset[date]:
    return frozenset(date.fromisoformat(str(v)) for v in values)


def _calendar(key: str, spec: dict[str, Any]) -> Calendar:
    kind = spec.get("kind")
    name = str(spec.get("name", key))
    if kind == "continuous":
        return ContinuousCalendar(name)
    if kind == "exchange":
        first, last = (date.fromisoformat(str(v)) for v in spec["coverage"])
        return ExchangeCalendar(
            name=name,
            timezone=str(spec["timezone"]),
            open_local=time.fromisoformat(str(spec["open"])),
            close_local=time.fromisoformat(str(spec["close"])),
            early_close_local=time.fromisoformat(str(spec["early_close"])),
            weekdays=frozenset(int(w) for w in spec["weekdays"]),
            holidays=_dates(spec.get("holidays", [])),
            early_closes=_dates(spec.get("early_closes", [])),
            first=first,
            last=last,
        )
    raise CalendarConfigError(f"{key}: unknown calendar kind {kind!r}")


def calendars_from(config: dict[str, Any]) -> dict[str, Calendar]:
    return {key: _calendar(key, spec) for key, spec in config.items()}


def load_calendars() -> dict[str, Calendar]:
    """All calendars defined in ``config/calendars.toml``, by key (``crypto``, ``xnys``)."""
    return calendars_from(load_toml("calendars"))
