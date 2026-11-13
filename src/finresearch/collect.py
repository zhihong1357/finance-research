"""Scheduled collection (Phase 2.2): one job per source, each on its own.

``config/collect.toml`` puts every source in a schedule group. When a group's
cron fires, the workflow asks ``plan`` which sources to run and starts one job
per source, so a failing source cannot stop the others.

A job (``run_source``) fetches, saves every response byte for byte in a fresh
local raw layer, pushes the new day folders to the remote store, and only then
parses: the raw layer is the one thing that cannot be recreated, so it is kept
even when the parser no longer understands what the source sends.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc
from finresearch.data.raw import RawStore
from finresearch.data.store import Limits, PushReport, Remote, push
from finresearch.sources import ADAPTERS


class CollectError(RuntimeError):
    """The configuration does not allow the requested run."""


def collect_config() -> dict[str, Any]:
    return load_toml("collect")


def plan(cfg: Mapping[str, Any], group: str) -> list[str]:
    """The enabled sources of ``group``, sorted."""
    if group not in cfg["groups"]:
        raise CollectError(f"unknown group {group!r} (known: {sorted(cfg['groups'])})")
    return sorted(
        name
        for name, src in cfg["sources"].items()
        if src["group"] == group and src.get("enabled", False)
    )


def group_for_cron(cfg: Mapping[str, Any], cron: str) -> str:
    """The group whose cron string is ``cron`` (what a scheduled run reports)."""
    for name, group in cfg["groups"].items():
        if group["cron"] == cron:
            return name
    raise CollectError(f"no group has the cron {cron!r}")


@dataclass(frozen=True)
class RunResult:
    source: str
    responses: int
    pushed: list[PushReport]


def run_source(
    source: str,
    cfg: Mapping[str, Any],
    remote: Remote,
    limits: Limits,
    work: Path,
    now: datetime,
) -> RunResult:
    """Fetch ``source`` once, keep the raw bytes, push them, then check they still parse.

    Raises ``SourceError`` if the fetch fails (nothing is written) and re-raises a
    parse failure after the raw bytes are safely pushed.
    """
    src = cfg["sources"].get(source)
    if src is None or not src.get("enabled", False):
        raise CollectError(f"{source} is not an enabled source in config/collect.toml")
    adapter = ADAPTERS[source]
    responses = adapter.fetch(src, ensure_utc(now))
    store = RawStore(work)
    records = [store.save(source, r.name, r.url, r.fetched_at, r.content) for r in responses]
    folders = sorted({r.path.parent.as_posix() for r in records})
    reports = [
        push(work, remote, folder, limits, f"{source}: {len(records)} response(s)")
        for folder in folders
    ]
    for record in records:
        adapter.parse(store.read(record), record.fetched_at)
    return RunResult(source, len(records), reports)
