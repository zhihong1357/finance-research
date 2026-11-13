# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed. All direct pyarrow calls live in this module, behind fully
# typed functions, so that the rest of the code base keeps strict type checking.
"""Point-in-time tables.

A ``PitTable`` is an Arrow table with three time columns and a set of key
columns (docs/architecture.md section 2.1):

* ``event_time``   - the moment or period the row describes
* ``available_at`` - when the public could first know it
* ``ingested_at``  - when we fetched it

Rows that share the keys and ``event_time`` are versions of one observation
(for example a macro figure and its later revisions). ``versions_as_of``
returns, for each observation, the latest version public at a given instant.

All three time columns are ``timestamp[us, tz=UTC]`` and never null.
"""

import io
import json
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc as ipc
import pyarrow.parquet as pq

from finresearch.core.fingerprint import Fingerprint, digest_bytes
from finresearch.core.time import ensure_utc

EVENT_TIME = "event_time"
AVAILABLE_AT = "available_at"
INGESTED_AT = "ingested_at"
TIME_COLUMNS = (EVENT_TIME, AVAILABLE_AT, INGESTED_AT)
TIME_TYPE = pa.timestamp("us", tz="UTC")
_META_NAME = b"finresearch.name"
_META_KEYS = b"finresearch.keys"


class TableError(ValueError):
    """A table that does not meet the point-in-time contract."""


@dataclass(frozen=True)
class PitTable:
    name: str
    keys: tuple[str, ...]
    table: pa.Table

    def __post_init__(self) -> None:
        names = set(self.table.column_names)
        for col in (*TIME_COLUMNS, *self.keys):
            if col not in names:
                raise TableError(f"{self.name}: missing column {col!r}")
            if self.table.column(col).null_count:
                raise TableError(f"{self.name}: column {col!r} has nulls")
        for col in TIME_COLUMNS:
            if self.table.schema.field(col).type != TIME_TYPE:
                raise TableError(f"{self.name}: {col!r} must be {TIME_TYPE}, got {self.table.schema.field(col).type}")
        if set(self.keys) & set(TIME_COLUMNS):
            raise TableError(f"{self.name}: time columns cannot be keys")
        if self.table.num_rows:
            ids = [*self.keys, EVENT_TIME, AVAILABLE_AT]
            counts = self.table.group_by(ids).aggregate([([], "count_all")])
            if pc.max(counts.column("count_all")).as_py() > 1:
                raise TableError(f"{self.name}: two rows share keys, event_time and available_at")
            late = pc.less(self.table.column(INGESTED_AT), self.table.column(AVAILABLE_AT))
            if pc.any(late).as_py():
                raise TableError(f"{self.name}: a row was ingested before it was available")

    @property
    def num_rows(self) -> int:
        return int(self.table.num_rows)

    def canonical(self) -> pa.Table:
        """Columns in name order, rows in key/time order, one chunk, no schema metadata."""
        order = [*self.keys, EVENT_TIME, AVAILABLE_AT, INGESTED_AT]
        t = self.table.select(sorted(self.table.column_names))
        t = t.sort_by([(c, "ascending") for c in order]).combine_chunks()
        return t.replace_schema_metadata(None)

    @cached_property
    def fingerprint(self) -> Fingerprint:
        """Fingerprint of the logical content: independent of row order, chunking and file format.

        Computed once per table object (tables are immutable)."""
        sink = io.BytesIO()
        canonical = self.canonical()
        with ipc.new_stream(sink, canonical.schema) as writer:
            writer.write_table(canonical)
        return digest_bytes(self.name.encode() + b"\0" + json.dumps(list(self.keys)).encode() + b"\0" + sink.getvalue())


def versions_as_of(t: PitTable, at: datetime) -> pa.Table:
    """For each observation, the latest version with ``available_at <= at``, in key/time order."""
    at = ensure_utc(at)
    known = t.table.filter(pc.less_equal(t.table.column(AVAILABLE_AT), pa.scalar(at, type=TIME_TYPE)))
    ids = [*t.keys, EVENT_TIME]
    if known.num_rows == 0:
        return known
    latest = known.group_by(ids).aggregate([(AVAILABLE_AT, "max")]).rename_columns([*ids, AVAILABLE_AT])
    out = known.join(latest, keys=[*ids, AVAILABLE_AT], join_type="inner")
    out = out.select(t.table.column_names)
    return out.sort_by([(c, "ascending") for c in ids]).combine_chunks()


def to_parquet(t: PitTable, path: Path) -> None:
    meta = {_META_NAME: t.name.encode(), _META_KEYS: json.dumps(list(t.keys)).encode()}
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t.table.replace_schema_metadata(meta), path)


def from_parquet(path: Path) -> PitTable:
    table = pq.read_table(path)
    meta = table.schema.metadata or {}
    if _META_NAME not in meta or _META_KEYS not in meta:
        raise TableError(f"{path}: not written by to_parquet (no point-in-time metadata)")
    keys = tuple(json.loads(meta[_META_KEYS]))
    return PitTable(meta[_META_NAME].decode(), keys, table.replace_schema_metadata(None))
