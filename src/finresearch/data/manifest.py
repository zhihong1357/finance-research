# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Archive manifests (Phase 2.4): what an exchange's public archive holds, file by file.

A redownloadable archive with official checksums is not copied into the raw
layer (docs/architecture.md 2.2): the raw layer keeps where each file is and
how to recognise it. A manifest entry is that record, one per file:

* ``dataset``, ``symbol``, ``period`` (``YYYY-MM`` or ``YYYY-MM-DD``) and ``url``;
* what the listing said about the file: ``size``, ``etag`` and
  ``last_modified`` (empty where the listing does not say);
* ``checksum_url``: the official checksum file, where the archive has one.

A full listing is large (hundreds of thousands of files per dataset), and most
of it is the same from one week to the next. So each listing is stored as an
*increment*: the entries that appeared (``added``), the entries whose listed
size, ETag or modification time differ from before (``changed``), and the
entries that are gone (``removed``, with their last known values). Increments
are written once, as ``manifests/<dataset>/<listing time>.parquet``, and never
changed. Replaying them in order gives exactly what any listing saw, so the
manifest is itself a point-in-time record of the archive.
"""

import re
from collections.abc import Iterable
from dataclasses import astuple, dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from finresearch.data.table import TIME_TYPE


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    dataset: str
    symbol: str
    period: str
    url: str
    size: int | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    checksum_url: str | None = None

    def listed_as(self) -> tuple[int | None, str | None, datetime | None, str | None]:
        """What a listing can see change about a file that keeps its URL."""
        return (self.size, self.etag, self.last_modified, self.checksum_url)


class Change(StrEnum):
    ADDED = "added"
    CHANGED = "changed"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class Row:
    change: Change
    entry: ManifestEntry


_ENTRY_FIELDS = [f.name for f in fields(ManifestEntry)]
SCHEMA = pa.schema(
    [
        ("change", pa.string()),
        ("dataset", pa.string()),
        ("symbol", pa.string()),
        ("period", pa.string()),
        ("url", pa.string()),
        ("size", pa.int64()),
        ("etag", pa.string()),
        ("last_modified", TIME_TYPE),
        ("checksum_url", pa.string()),
    ]
)
PREFIX = "manifests"
_STAMP = "%Y%m%dT%H%M%SZ"


_INCREMENT = re.compile(r"/(\d{8}T\d{6}Z)\.parquet$")


def increment_path(dataset: str, listed: datetime) -> str:
    """Where the increment of a listing that started at ``listed`` (UTC) is stored."""
    return f"{PREFIX}/{dataset}/{listed:{_STAMP}}.parquet"


def increment_time(path: str) -> datetime | None:
    """When the listing behind an increment started, from its path."""
    m = _INCREMENT.search(path)
    return datetime.strptime(m.group(1), _STAMP).replace(tzinfo=UTC) if m else None


def diff(before: dict[str, ManifestEntry], listing: Iterable[ManifestEntry]) -> list[Row]:
    """The increment that turns ``before`` (URL -> entry) into ``listing``."""
    now = {e.url: e for e in listing}
    rows = [Row(Change.ADDED, e) for u, e in now.items() if u not in before]
    rows += [
        Row(Change.CHANGED, e)
        for u, e in now.items()
        if u in before and before[u].listed_as() != e.listed_as()
    ]
    rows += [Row(Change.REMOVED, e) for u, e in before.items() if u not in now]
    return sorted(rows, key=lambda r: r.entry.url)


def apply(state: dict[str, ManifestEntry], rows: Iterable[Row]) -> None:
    """Bring ``state`` forward by one increment."""
    for row in rows:
        if row.change is Change.REMOVED:
            state.pop(row.entry.url, None)
        else:
            state[row.entry.url] = row.entry


def replay(increments: Iterable[list[Row]]) -> dict[str, ManifestEntry]:
    """What the archive held at the last of ``increments`` (given oldest first)."""
    state: dict[str, ManifestEntry] = {}
    for rows in increments:
        apply(state, rows)
    return state


def write_increment(rows: list[Row], path: Path) -> None:
    """Rows sorted by URL, so the same increment always gives the same bytes."""
    ordered = sorted(rows, key=lambda r: r.entry.url)
    data: dict[str, list[object]] = {"change": [str(r.change) for r in ordered]}
    columns = list(zip(*(astuple(r.entry) for r in ordered), strict=True))
    for i, name in enumerate(_ENTRY_FIELDS):
        data[name] = list(columns[i]) if columns else []
    table = pa.table({n: pa.array(data[n], type=SCHEMA.field(n).type) for n in SCHEMA.names})
    path.parent.mkdir(parents=True, exist_ok=True)
    # URLs are sorted, so neighbours share long prefixes: delta encoding stores
    # the shared part once. The short repeated columns use a dictionary.
    pq.write_table(
        table,
        path,
        compression="zstd",
        # The stubs say bool; pyarrow also takes a list of columns (documented).
        use_dictionary=["change", "dataset", "symbol", "period"],  # pyright: ignore[reportArgumentType]
        column_encoding={"url": "DELTA_BYTE_ARRAY", "checksum_url": "DELTA_BYTE_ARRAY"},
    )


def read_increment(path: Path) -> list[Row]:
    rows = pq.read_table(path, schema=SCHEMA).to_pylist()
    return [
        Row(Change(r.pop("change")), ManifestEntry(**{k: r[k] for k in _ENTRY_FIELDS}))
        for r in rows
    ]
