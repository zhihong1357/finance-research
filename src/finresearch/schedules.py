"""Catching up on scheduled runs GitHub did not start (Phase 2.2).

GitHub's scheduler is best effort: a scheduled run can start hours late or
not at all. On 2026-11-14 the collection workflow, due about 13 times a day,
started 6 times; on 2026-11-15 the table builds (``revised.yml``) did not
start once (docs/architecture.md 7.3). A missed snapshot cannot be fetched
again, so waiting for the scheduler is not enough.

``catch_up`` runs at the start of every collection run and after every
monitor pass. For each workflow in ``config/schedules.toml`` and each of its
crons it finds the latest instant the cron was due (``previous``), at least
``grace_minutes`` ago. If no run of that workflow was created since then, it
starts one with ``workflow_dispatch``. The job's own ``GITHUB_TOKEN`` may do
this: runs it starts by dispatch are the documented exception to "events made
with GITHUB_TOKEN start no workflows".

The collection workflow has one cron per group (``config/collect.toml``), so a
run only counts for a group if its title names the group or its cron: the
workflow's ``run-name`` puts either there. A cancelled run does not count. A
due instant older than ``lookback_hours`` is ignored.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from finresearch.core.time import ensure_utc

_FIELDS = 5
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_SUNDAY_ALIAS = 7
_DAYS_BACK = 400
_CRON_LINE = re.compile(r'^\s*-\s*cron:\s*"([^"]+)"', re.M)
_IGNORED = {"cancelled", "skipped"}


class ScheduleError(ValueError):
    """A cron expression this module does not read, or a workflow without crons."""


@dataclass(frozen=True)
class Cron:
    """A five-field cron expression: numbers, ``*``, ``*/n``, ``a-b`` and lists."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, text: str) -> Cron:
        parts = text.split()
        if len(parts) != _FIELDS:
            raise ScheduleError(f"{text!r}: a cron has five fields")
        sets = [_field(p, lo, hi, text) for p, (lo, hi) in zip(parts, _RANGES, strict=True)]
        weekdays = frozenset(0 if d == _SUNDAY_ALIAS else d for d in sets[4])
        minutes, hours, days, months, _ = sets
        return cls(text, minutes, hours, days, months, weekdays, parts[2] == "*", parts[4] == "*")

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        weekday = (d.weekday() + 1) % 7
        in_days, in_weekdays = d.day in self.days, weekday in self.weekdays
        if self.any_day or self.any_weekday:  # cron: both restricted means either may match
            return in_days and in_weekdays
        return in_days or in_weekdays

    def previous(self, t: datetime) -> datetime:
        """The latest instant at or before ``t`` (UTC) at which this cron is due."""
        t = ensure_utc(t).replace(second=0, microsecond=0)
        for back in range(_DAYS_BACK):
            d = t.date() - timedelta(days=back)
            if not self.day_matches(d):
                continue
            for h in sorted(self.hours, reverse=True):
                for m in sorted(self.minutes, reverse=True):
                    at = datetime.combine(d, time(h, m), tzinfo=t.tzinfo)
                    if at <= t:
                        return at
        raise ScheduleError(f"{self.text!r} is not due in the last {_DAYS_BACK} days")


def _field(part: str, lo: int, hi: int, text: str) -> frozenset[int]:
    out: set[int] = set()
    for item in part.split(","):
        body, _, step_text = item.partition("/")
        step = int(step_text) if step_text else 1
        if body == "*":
            first, last = lo, hi
        elif "-" in body:
            a, b = body.split("-", 1)
            first, last = int(a), int(b)
        else:
            first = last = int(body)
        if not (lo <= first <= last <= hi) or step < 1:
            raise ScheduleError(f"{text!r}: {item!r} is outside {lo}-{hi}")
        out.update(range(first, last + 1, step))
    return frozenset(out)


def workflow_crons(path: Path) -> list[str]:
    """The crons of a workflow file, in the order written."""
    found = _CRON_LINE.findall(path.read_text(encoding="utf-8"))
    if not found:
        raise ScheduleError(f"{path} has no cron")
    return found


@dataclass(frozen=True)
class Run:
    title: str
    created_at: datetime
    conclusion: str | None


class Actions(Protocol):
    def runs(self, workflow: str, since: datetime) -> list[Run]: ...
    def dispatch(self, workflow: str, ref: str, inputs: Mapping[str, str]) -> None: ...


@dataclass(frozen=True)
class Due:
    workflow: str
    cron: str
    key: str | None  # the collection group the cron belongs to
    at: datetime


def due(cfg: Mapping[str, Any], root: Path, now: datetime, groups: Mapping[str, str]) -> list[Due]:
    """Each configured workflow's latest due instant per cron, inside the look-back window.

    ``groups`` maps a cron of the collection workflow to its group.
    """
    now = ensure_utc(now)
    latest = now - timedelta(minutes=float(cfg["grace_minutes"]))
    oldest = now - timedelta(hours=float(cfg["lookback_hours"]))
    out: list[Due] = []
    for name, w in sorted(dict(cfg["workflows"]).items()):
        by_group = bool(w.get("by_group", False))
        for cron in workflow_crons(root / ".github" / "workflows" / name):
            at = Cron.parse(cron).previous(latest)
            if at < oldest:
                continue
            key = groups.get(cron) if by_group else None
            if by_group and key is None:
                raise ScheduleError(f"{name}: no group has the cron {cron!r}")
            out.append(Due(name, cron, key, at))
    return out


def ran(d: Due, runs: Sequence[Run]) -> bool:
    """Whether a run that counts for ``d`` was created at or after its due instant."""
    for r in runs:
        if r.conclusion in _IGNORED or ensure_utc(r.created_at) < d.at:
            continue
        if d.key is None or r.title.endswith((f" {d.key}", f" {d.cron}")):
            return True
    return False


def catch_up(
    cfg: Mapping[str, Any],
    root: Path,
    actions: Actions,
    now: datetime,
    groups: Mapping[str, str],
    ref: str,
) -> list[str]:
    """Start every missed run. Returns log lines (names and times only)."""
    lines: list[str] = []
    for d in due(cfg, root, now, groups):
        label = f"{d.workflow}{' ' + d.key if d.key else ''} (due {d.at:%Y-%m-%d %H:%M} UTC)"
        if ran(d, actions.runs(d.workflow, d.at)):
            lines.append(f"{label}: ran")
            continue
        inputs = {
            str(k): str(v) for k, v in dict(cfg["workflows"][d.workflow].get("inputs", {})).items()
        }
        if d.key is not None:
            inputs["group"] = d.key
        actions.dispatch(d.workflow, ref, inputs)
        lines.append(f"{label}: missed, started")
    return lines
