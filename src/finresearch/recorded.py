# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""The canonical layer of recorded data (Phase 2.10).

Some data exist only because we recorded them: snapshots of Deribit's book
summary and Hyperliquid's asset contexts, and Deribit's DVOL candles (its
1-minute history is kept for about six months). The raw layer has every
response as fetched; this module turns them into month-partitioned
point-in-time tables, read through the same door as U1.

* **One pass.** Every stored response is parsed once and its rows sorted into
  the months of their ``event_time``. A snapshot's rows fall in the month it
  was fetched; a DVOL backfill fetched today holds candles of past years.
* **Written once.** A month is written when it has ended and ``settle_days``
  more have passed (later fetches can still carry its last rows), and never
  again: a month already stored is left alone.
* **One row per observation.** The same row seen in several fetches (DVOL
  candles are fetched again for ``lookback_days``) is kept as first seen: the
  earliest ``ingested_at``. If the repeats disagree, the first is kept and the
  card says how many disagreed.
* **Rebuilt from its card.** The card lists every response that gave a row,
  with its fingerprint. ``verify`` pulls exactly those, builds the month again
  and compares content fingerprints (``same`` or ``changed``).
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc, month_start
from finresearch.data.fixed import Column
from finresearch.data.partitioned import PartitionCard, PartitionWriter, month_dir
from finresearch.data.raw import RawRecord, RawStore
from finresearch.data.store import Limits, Remote, pull, push
from finresearch.data.table import AVAILABLE_AT, EVENT_TIME, INGESTED_AT, PitTable
from finresearch.sources import ADAPTERS


class RecordedError(RuntimeError):
    """A table that is not configured, or responses that cannot be built."""


@dataclass(frozen=True)
class Spec:
    table: str
    source: str
    format: int
    part: str
    settle_days: int
    whole_raw: bool  # rows of a month can come from fetches long after it (a backfill)
    first: date  # the first month with rows


def specs(cfg: Mapping[str, Any] | None = None) -> dict[str, Spec]:
    cfg = load_toml("recorded") if cfg is None else cfg
    return {
        name: Spec(
            name,
            str(t["source"]),
            int(t["format"]),
            str(t["part"]),
            int(t["settle_days"]),
            bool(t["whole_raw"]),
            date.fromisoformat(str(t["first"])),
        )
        for name, t in dict(cfg["tables"]).items()
    }


def spec(table: str, cfg: Mapping[str, Any] | None = None) -> Spec:
    found = specs(cfg)
    if table not in found:
        raise RecordedError(f"{table} is not a recorded table (config/recorded.toml)")
    return found[table]


@dataclass
class Month:
    """The rows of one month and the responses they came from."""

    tables: list[PitTable]
    inputs: dict[str, str]  # raw path -> fingerprint


def sort_into_months(s: Spec, store: RawStore, records: Sequence[RawRecord]) -> dict[date, Month]:
    """Parse each response once; rows go to the month of their event_time."""
    adapter = ADAPTERS.get(s.source)
    if adapter is None:
        raise RecordedError(f"{s.source} has no adapter")
    months: dict[date, Month] = defaultdict(lambda: Month([], {}))
    for r in records:
        t = adapter.parse_record(store.read(r), r.fetched_at, r.url)
        if t.name != s.table:
            raise RecordedError(f"{s.source} parses into {t.name}, not {s.table}")
        if t.num_rows == 0:
            continue
        starts = pc.floor_temporal(t.table.column(EVENT_TIME), unit="month")
        for start in pc.unique(starts).to_pylist():
            if start is None:
                continue
            m = ensure_utc(start).date()
            rows = t.table.filter(pc.equal(starts, pa.scalar(start, starts.type)))
            months[m].tables.append(PitTable(t.name, t.keys, rows))
            months[m].inputs[r.path.as_posix()] = str(r.fingerprint)
    return dict(months)


def first_seen(tables: Sequence[PitTable]) -> tuple[PitTable, int]:
    """One row per observation and version, the earliest ingested; and repeats that disagreed."""
    name, keys = tables[0].name, tables[0].keys
    table = pa.concat_tables([t.table for t in tables], promote_options="default")
    # A column that is empty in every response of a month parses as type null; the
    # recorded fields are numbers, so it is stored as float64 like the other months.
    for i, f in enumerate(table.schema):
        if pa.types.is_null(f.type):
            table = table.set_column(i, f.name, table.column(i).cast(pa.float64()))
    ids = [*keys, EVENT_TIME, AVAILABLE_AT]
    table = table.sort_by([(c, "ascending") for c in [*ids, INGESTED_AT]]).combine_chunks()
    n = table.num_rows
    if n < 2:  # noqa: PLR2004 - a repeat needs two rows
        return PitTable(name, keys, table), 0
    changed: Column = pa.array([False] * (n - 1))
    for c in ids:
        col = table.column(c)
        changed = pc.or_(changed, pc.not_equal(col[1:], col[:-1]))
    new = pa.concat_arrays([pa.array([True]), pa.chunked_array([changed]).combine_chunks()])
    values = [c for c in table.column_names if c not in (*ids, INGESTED_AT)]
    differ = pa.array([False] * (n - 1))
    for c in values:
        col = table.column(c)
        same = pc.or_kleene(
            pc.equal(col[1:], col[:-1]), pc.and_(pc.is_null(col[1:]), pc.is_null(col[:-1]))
        )
        differ = pc.or_(differ, pc.invert(pc.fill_null(same, pa.scalar(False))))
    repeats = pc.invert(new.slice(1))
    disagreed = int(pc.sum(pc.and_(repeats, differ).cast(pa.int64())).as_py() or 0)
    return PitTable(name, keys, table.filter(new)), disagreed


def settled(month: date, now: datetime, settle_days: int) -> bool:
    """Whether ``month`` ended at least ``settle_days`` before ``now``."""
    end = month_start(month, 1)
    return ensure_utc(now).date() >= end + timedelta(days=settle_days)


def write_month(s: Spec, month: date, m: Month, local: Path, row_group_rows: int) -> PartitionCard:
    table, disagreed = first_seen(m.tables)
    writer = PartitionWriter(local, s.table, table.keys, s.format, month, s.part, row_group_rows)
    writer.add(table)
    notes = (
        [f"{disagreed} repeated row(s) disagreed with the first seen; the first is kept"]
        if disagreed
        else []
    )
    return writer.close(m.inputs, notes)


def stored_months(s: Spec, remote: Remote) -> set[date]:
    names = {
        p.split("/")[-2]
        for p in remote.list(f"canonical/{s.table}/v{s.format}")
        if p.endswith(f"/{s.part}.json")
    }
    return {date(int(n[:4]), int(n[5:7]), 1) for n in names}


@dataclass(frozen=True)
class Built:
    table: str
    responses: int
    months_written: list[str]
    months_waiting: list[str]  # not settled yet
    pushed: int


def _pull_raw(s: Spec, remote: Remote, limits: Limits, work: Path, months: Sequence[date]) -> None:
    if s.whole_raw:
        pull(remote, work, f"raw/{s.source}", limits)
        return
    for m in months:  # a snapshot's rows are in the month it was fetched
        pull(remote, work, f"raw/{s.source}/{m:%Y/%m}", limits)


def build(
    s: Spec,
    remote: Remote,
    limits: Limits,
    work: Path,
    now: datetime,
    row_group_rows: int,
) -> Built:
    """Write every settled month not stored yet, from the spec's first month on."""
    stored = stored_months(s, remote)
    candidates: list[date] = []
    m = month_start(s.first)
    while settled(m, now, s.settle_days):
        if m not in stored:
            candidates.append(m)
        m = month_start(m, 1)
    if not candidates and not s.whole_raw:
        return Built(s.table, 0, [], [], 0)
    _pull_raw(s, remote, limits, work, candidates)
    store = RawStore(work)
    records = store.records(s.source)
    months = sort_into_months(s, store, records)
    written: list[str] = []
    waiting: list[str] = []
    out = work / "built"
    for month in sorted(months):
        if month in stored:
            continue
        if not settled(month, now, s.settle_days):
            waiting.append(f"{month:%Y-%m}")
            continue
        write_month(s, month, months[month], out, row_group_rows)
        written.append(f"{month:%Y-%m}")
    pushed = 0
    if written:
        report = push(
            out, remote, "canonical", limits, f"recorded: {s.table}, {len(written)} month(s)"
        )
        pushed = len(report.added)
    return Built(s.table, len(records), written, waiting, pushed)


def verify(
    s: Spec, month: date, remote: Remote, limits: Limits, work: Path, row_group_rows: int
) -> str:
    """Build a stored month again from the responses on its card: ``same`` or ``changed``."""
    folder = month_dir(s.table, s.format, month)
    card_path = f"{folder}/{s.part}.json"
    pull(remote, work / "stored", folder, limits)
    local_card = work / "stored" / card_path
    if not local_card.is_file():
        raise RecordedError(f"{s.table} has no partition for {month:%Y-%m}")
    card = PartitionCard.from_json(local_card.read_text(encoding="utf-8"))
    raw = work / "raw"
    for path in card.inputs:
        pull(remote, raw, path.rsplit("/", 1)[0], limits)
    store = RawStore(raw)
    wanted = set(card.inputs)
    records = [r for r in store.records(s.source) if r.path.as_posix() in wanted]
    if {r.path.as_posix(): str(r.fingerprint) for r in records} != dict(card.inputs):
        raise RecordedError(f"{card.name}: the responses on the card are not all in the raw layer")
    months = sort_into_months(s, store, records)
    if month not in months:
        return "changed"
    rebuilt = write_month(s, month, months[month], work / "rebuilt", row_group_rows)
    return "same" if rebuilt.fingerprint == card.fingerprint else "changed"
