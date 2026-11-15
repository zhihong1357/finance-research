"""Tables of revised series built from collected responses (Phase 2.6.3, decision 0013).

A collected source such as ``nyfed.reference_rates`` leaves one raw response
per fetch. ``build`` turns every response stored so far into one table: each
response is parsed into versions (``Adapter.parse``), the fetches are merged
(``vintages.merge``: a version seen in several fetches is kept once) and
repeated values are dropped (``vintages.compact``). The table is written as a
full build (``data/builds.py``) unless its content equals the latest build.

``verify`` is the rebuild check of 2.5.7 for these tables: it rebuilds the
latest build from exactly the responses its card lists and compares content
fingerprints, without writing anything.

The tables are listed in ``config/revised.toml``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc
from finresearch.data import builds, vintages
from finresearch.data.raw import RawRecord, RawStore
from finresearch.data.store import Limits, Remote, pull, push
from finresearch.data.table import PitTable
from finresearch.sources import ADAPTERS


class RevisedError(RuntimeError):
    """A table that is not configured, or raw responses that cannot be built into it."""


@dataclass(frozen=True)
class Spec:
    table: str
    source: str
    format: int
    part: str
    conflicts: vintages.Conflicts = vintages.Conflicts.REFUSE


def specs(cfg: Mapping[str, Any] | None = None) -> dict[str, Spec]:
    cfg = load_toml("revised") if cfg is None else cfg
    return {
        name: Spec(
            name,
            str(t["source"]),
            int(t["format"]),
            str(t["part"]),
            vintages.Conflicts(str(t.get("conflicts", "refuse"))),
        )
        for name, t in dict(cfg["tables"]).items()
    }


def spec(table: str, cfg: Mapping[str, Any] | None = None) -> Spec:
    found = specs(cfg).get(table)
    if found is None:
        raise RevisedError(f"{table} is not a table in config/revised.toml")
    return found


def assemble(s: Spec, store: RawStore, records: list[RawRecord]) -> PitTable:
    """The table from ``records``: parsed, merged, repeats dropped."""
    adapter = ADAPTERS.get(s.source)
    if adapter is None:
        raise RevisedError(f"{s.source} has no adapter")
    tables = [adapter.parse_record(store.read(r), r.fetched_at, r.url) for r in records]
    for t in tables:
        if t.name != s.table:
            raise RevisedError(f"{s.source} gave a table {t.name!r}, not {s.table!r}")
    return vintages.compact(vintages.merge(s.table, tables, s.conflicts))


def _inputs(records: list[RawRecord]) -> dict[str, str]:
    return {r.path.as_posix(): str(r.fingerprint) for r in records}


@dataclass(frozen=True)
class Built:
    table: str
    responses: int
    rows: int
    card: builds.BuildCard | None  # None when the content equals the latest build
    pushed: int


def build(s: Spec, remote: Remote, limits: Limits, work: Path, now: datetime) -> Built:
    """Build ``s`` from every stored response; push it unless it equals the latest build."""
    pull(remote, work, f"raw/{s.source}", limits)
    store = RawStore(work)
    records = store.records(s.source)
    if not records:
        raise RevisedError(f"the raw layer has nothing for {s.source}")
    latest = builds.latest_card_path(remote, s.table, s.format, s.part, limits)
    card = builds.read_card(remote, latest, limits) if latest is not None else None
    if card is not None and card.inputs == _inputs(records):
        # Nothing collected since the latest build: the same responses give the same table.
        return Built(s.table, len(records), card.rows, None, 0)
    t = assemble(s, store, records)
    if card is not None and card.fingerprint == str(t.fingerprint):
        return Built(s.table, len(records), t.num_rows, None, 0)
    made = builds.write_build(t, work, s.format, s.part, ensure_utc(now), _inputs(records))
    folder = builds.build_dir(s.table, s.format, ensure_utc(now))
    report = push(work, remote, folder, limits, f"{s.table}: build {made.built}, {made.rows} rows")
    return Built(s.table, len(records), t.num_rows, made, len(report.added))


class Verdict(StrEnum):
    SAME = "same"
    INPUTS_CHANGED = "inputs changed"
    DIFFERENT = "different"


@dataclass(frozen=True)
class Check:
    table: str
    built: str
    verdict: Verdict
    detail: str


def verify(s: Spec, remote: Remote, limits: Limits, work: Path) -> Check:
    """Rebuild the latest build from the responses its card lists; compare fingerprints."""
    latest = builds.latest_card_path(remote, s.table, s.format, s.part, limits)
    if latest is None:
        raise RevisedError(f"{s.table} has no build yet")
    card = builds.read_card(remote, latest, limits)
    pull(remote, work, f"raw/{s.source}", limits)
    store = RawStore(work)
    records = [r for r in store.records(s.source) if r.path.as_posix() in card.inputs]
    found = _inputs(records)
    if found != card.inputs:
        missing = len(set(card.inputs) - set(found))
        changed = sum(1 for p, f in found.items() if card.inputs[p] != f)
        detail = f"{missing} response(s) missing, {changed} changed"
        return Check(s.table, card.built, Verdict.INPUTS_CHANGED, detail)
    t = assemble(s, store, records)
    if str(t.fingerprint) == card.fingerprint:
        return Check(s.table, card.built, Verdict.SAME, f"{t.num_rows} rows")
    detail = f"{t.num_rows} rows now, {card.rows} on the card"
    return Check(s.table, card.built, Verdict.DIFFERENT, detail)
