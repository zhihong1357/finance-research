# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""The acceptance check of Phase 2.6 (step 2.6.8), run on the real data.

Two promises are checked, as docs/roadmap.md 2.6.8 states them:

* **Every series has three times.** The table type itself (``PitTable``)
  refuses a row without ``event_time``, ``available_at`` and ``ingested_at``,
  and a row fetched before it was public, so loading the real table is part of
  the check: such a row fails it with ``TableError``. What the type does not
  check is counted here: nothing may be public before it can be
  (``available_at`` at least the end of the period for a revised series, at
  least ``event_time`` for an event or a bar).
* **A revision is seen when it happened, not before.** For a sample of revised
  observations, the point-in-time door is asked just before and at the instant
  the new version became public: the first answer must be the old value, the
  second the new one.

Revised series are checked on their latest full build (``config/revised.toml``).
Recorded sources that have no build yet (``config/acceptance.toml``) are parsed
from every stored raw response and checked for the three times.

Results carry counts only: the data are third-party (decision 0008).
"""

import itertools
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc
from finresearch.data import builds, vintages
from finresearch.data.fixed import Column
from finresearch.data.pit import Clock, PointInTime
from finresearch.data.raw import RawStore
from finresearch.data.store import Limits, Remote, pull
from finresearch.data.table import AVAILABLE_AT, EVENT_TIME, TIME_TYPE, PitTable
from finresearch.revised import RevisedError, Spec, spec, specs
from finresearch.sources import ADAPTERS

_JUST_BEFORE = timedelta(microseconds=1)


class AcceptanceError(RuntimeError):
    """A target that is not configured, or that has nothing to check yet."""


@dataclass(frozen=True)
class Times:
    rows: int
    early: int  # public before the period ends (or before the event)

    @property
    def ok(self) -> bool:
        return self.early == 0


@dataclass(frozen=True)
class Probe:
    revised: int  # observations with more than one version
    probed: int  # revisions asked about
    wrong: int  # answers that were not the expected version

    @property
    def ok(self) -> bool:
        return self.wrong == 0


@dataclass(frozen=True)
class Result:
    target: str
    times: Times
    probe: Probe | None  # None for a target without revised series

    @property
    def ok(self) -> bool:
        return self.times.ok and (self.probe is None or self.probe.ok)


def three_times(t: PitTable) -> Times:
    """Count the rows of ``t`` public before they could have been."""
    table = t.table
    early = pc.less(table.column(AVAILABLE_AT), _public_floor(table))
    return Times(table.num_rows, int(pc.sum(early.cast(pa.int64())).as_py() or 0))


def _public_floor(table: pa.Table) -> Column:
    """The earliest a row may be public: its period's end for a revised series, else its event."""
    events = table.column(EVENT_TIME).combine_chunks()
    if vintages.FREQUENCY not in table.column_names:
        return events
    frequencies = [str(f) for f in table.column(vintages.FREQUENCY).to_pylist()]
    starts = [ensure_utc(e) for e in events.to_pylist() if e is not None]
    ends = [_period_end(e, f) for e, f in zip(starts, frequencies, strict=True)]
    return pa.array(ends, TIME_TYPE)


def _period_end(event: datetime, frequency: str) -> datetime:
    start = ensure_utc(event).date()
    end: date = vintages.period_end(start, vintages.Frequency(frequency))
    return datetime(end.year, end.month, end.day, tzinfo=event.tzinfo)


def probe(t: PitTable, sample: int, seed: int) -> Probe:
    """Ask the door about ``sample`` revisions of ``t``, picked with ``seed``."""
    table = t.table.sort_by(
        [(vintages.SERIES, "ascending"), (EVENT_TIME, "ascending"), (AVAILABLE_AT, "ascending")]
    )
    rows = table.select(
        [vintages.SERIES, EVENT_TIME, AVAILABLE_AT, vintages.VALUE, vintages.VALUE_SCALE]
    ).to_pylist()
    pairs: list[tuple[str, datetime, Decimal, datetime, Decimal]] = []
    revised: set[tuple[str, datetime]] = set()
    for old, new in itertools.pairwise(rows):
        if (old[vintages.SERIES], old[EVENT_TIME]) != (new[vintages.SERIES], new[EVENT_TIME]):
            continue
        a, b = _value(old), _value(new)
        if a == b:
            continue  # a repeated value is not a revision (compact drops these)
        revised.add((old[vintages.SERIES], old[EVENT_TIME]))
        pairs.append(
            (old[vintages.SERIES], ensure_utc(old[EVENT_TIME]), a, ensure_utc(new[AVAILABLE_AT]), b)
        )
    rng = random.Random(seed)  # noqa: S311 - a reproducible sample, not a secret
    picked = rng.sample(pairs, min(sample, len(pairs)))
    if not picked:
        return Probe(len(revised), 0, 0)
    limit = max(p[3] for p in picked)
    door = PointInTime(Clock(limit=limit), [t])
    wrong = 0
    for series, event, before, at, after in picked:
        wrong += _seen(door, t.name, series, event, at - _JUST_BEFORE) != before
        wrong += _seen(door, t.name, series, event, at) != after
    return Probe(len(revised), len(picked), wrong)


def _value(row: Mapping[str, Any]) -> Decimal:
    return Decimal(row[vintages.VALUE]).scaleb(-row[vintages.VALUE_SCALE]).normalize()


def _seen(
    door: PointInTime, name: str, series: str, event: datetime, at: datetime
) -> Decimal | None:
    """The value of one observation, as the door answers at ``at``."""
    snap = door.window(name, event, at, where={vintages.SERIES: {series}})
    rows = [r for r in snap.table.to_pylist() if ensure_utc(r[EVENT_TIME]) == event]
    if len(rows) != 1:
        return None
    return _value(rows[0])


def targets(
    cfg: Mapping[str, Any] | None = None, revised_cfg: Mapping[str, Any] | None = None
) -> list[str]:
    """Everything the check covers: the revised tables, then the recorded sources."""
    cfg = load_toml("acceptance") if cfg is None else cfg
    return [*sorted(specs(revised_cfg)), *[str(s) for s in cfg["recorded"]]]


def run(
    target: str,
    remote: Remote,
    limits: Limits,
    work: Path,
    seed: int,
    cfg: Mapping[str, Any] | None = None,
    revised_cfg: Mapping[str, Any] | None = None,
) -> Result:
    cfg = load_toml("acceptance") if cfg is None else cfg
    if target in specs(revised_cfg):
        return _revised(spec(target, revised_cfg), remote, limits, work, int(cfg["sample"]), seed)
    if target in [str(s) for s in cfg["recorded"]]:
        return _recorded(target, remote, limits, work)
    raise AcceptanceError(f"{target} is neither a revised table nor a recorded source")


def _revised(s: Spec, remote: Remote, limits: Limits, work: Path, sample: int, seed: int) -> Result:
    latest = builds.latest_card_path(remote, s.table, s.format, s.part, limits)
    if latest is None:
        raise AcceptanceError(f"{s.table} has no build yet")
    _, t = builds.load(remote, work, latest, limits)
    return Result(s.table, three_times(t), probe(t, sample, seed))


def _recorded(source: str, remote: Remote, limits: Limits, work: Path) -> Result:
    adapter = ADAPTERS.get(source)
    if adapter is None:
        raise RevisedError(f"{source} has no adapter")
    pull(remote, work, f"raw/{source}", limits)
    store = RawStore(work)
    records = store.records(source)
    if not records:
        raise AcceptanceError(f"the raw layer has nothing for {source}")
    rows = early = 0
    for r in records:
        found = three_times(adapter.parse_record(store.read(r), r.fetched_at, r.url))
        rows, early = rows + found.rows, early + found.early
    return Result(source, Times(rows, early), None)
