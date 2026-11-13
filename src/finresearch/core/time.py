"""Time.

Every instant in this system is a timezone-aware ``datetime`` in UTC. Naive
datetimes are rejected at the boundary instead of being guessed at.

Integer timestamps (as exchanges publish them) are microseconds or
milliseconds since the Unix epoch. Converting to a coarser unit always floors
toward the past, so rounding can never move a timestamp into the future.

Local time zones come from the pinned ``tzdata`` package rather than the host
system, so daylight-saving rules are identical on every machine.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib.resources import files
from zoneinfo import ZoneInfo

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROS_PER_SECOND = 1_000_000
_MICROS_PER_MILLI = 1_000
_SECONDS_PER_DAY = 86_400


class NaiveDatetimeError(ValueError):
    """A datetime without a time zone reached code that needs an instant."""


def ensure_utc(t: datetime) -> datetime:
    """Return ``t`` converted to UTC; raise if it has no time zone."""
    if t.tzinfo is None or t.utcoffset() is None:
        raise NaiveDatetimeError(f"naive datetime {t!r}: every instant must carry a time zone")
    return t.astimezone(UTC)


def utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime:
    """A UTC instant from calendar fields."""
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)


def parse_utc(text: str) -> datetime:
    """Parse ISO 8601 text that states its offset (``Z`` or ``+hh:mm``) into UTC."""
    return ensure_utc(datetime.fromisoformat(text))


def format_utc(t: datetime) -> str:
    """Canonical text form: ``2026-11-13T03:15:13.000000Z``."""
    return ensure_utc(t).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def to_epoch_us(t: datetime) -> int:
    """Microseconds since the Unix epoch (exact integer arithmetic)."""
    delta = ensure_utc(t) - EPOCH
    return (delta.days * _SECONDS_PER_DAY + delta.seconds) * _MICROS_PER_SECOND + delta.microseconds


def from_epoch_us(us: int) -> datetime:
    return EPOCH + timedelta(microseconds=us)


def to_epoch_ms(t: datetime) -> int:
    """Milliseconds since the Unix epoch, floored toward the past."""
    return to_epoch_us(t) // _MICROS_PER_MILLI


def from_epoch_ms(ms: int) -> datetime:
    return from_epoch_us(ms * _MICROS_PER_MILLI)


def floor_time(t: datetime, step: timedelta) -> datetime:
    """Start of the ``step``-long bucket containing ``t``; buckets are aligned to the epoch."""
    step_us = _micros(step)
    if step_us <= 0:
        raise ValueError(f"step must be positive, got {step}")
    us = to_epoch_us(t)
    return from_epoch_us(us - us % step_us)


def _micros(d: timedelta) -> int:
    return (d.days * _SECONDS_PER_DAY + d.seconds) * _MICROS_PER_SECOND + d.microseconds


@cache
def zone(name: str) -> ZoneInfo:
    """IANA time zone loaded from the pinned ``tzdata`` package (not the host system)."""
    resource = files("tzdata.zoneinfo").joinpath(*name.split("/"))
    with resource.open("rb") as fh:
        return ZoneInfo.from_file(fh, key=name)


@dataclass(frozen=True, slots=True)
class Interval:
    """Half-open span of time ``[start, end)`` in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start))
        object.__setattr__(self, "end", ensure_utc(self.end))
        if not self.start < self.end:
            raise ValueError(f"interval must have start < end, got {self.start} .. {self.end}")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, t: datetime) -> bool:
        return self.start <= ensure_utc(t) < self.end

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end
