# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Month-partitioned point-in-time tables (Phase 2.5.2): large tables, read a window at a time.

U1's 1-minute bars run to billions of rows, far more than fit in memory. Such a
table is stored as partitions, one per month of ``event_time`` and *part* (for
the bars, the exchange)::

    canonical/<table>/v<format>/<YYYY-MM>/<part>.parquet
    canonical/<table>/v<format>/<YYYY-MM>/<part>.json   the partition's card

The card records the table's keys, the row count, the content fingerprint, the
file's SHA-256, whether every observation has a single version, the input
files the partition was built from, and notes (such as impossible bars that
were kept). ``PartitionWriter`` builds a partition a piece at a time, so a
month of every symbol never has to be in memory at once. Partitions are written once (the remote
store is append-only); ``format`` goes up when the table's layout changes, so a
new layout never overwrites an old one.

A ``PartitionedTable`` answers a query from the partitions that overlap its
window only, checks each file against its card before reading it, and pulls
missing partitions from the remote store first. Row-group statistics let a
query for a few symbols skip most of a file.
"""

import hashlib
import io
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc as ipc
import pyarrow.parquet as pq

from finresearch.core.fingerprint import Fingerprint, digest_file
from finresearch.core.time import ensure_utc, month_start
from finresearch.data.store import Limits, Remote, pull
from finresearch.data.table import (
    AVAILABLE_AT,
    EVENT_TIME,
    INGESTED_AT,
    TIME_COLUMNS,
    TIME_TYPE,
    PitTable,
    latest_versions,
)

ROOT = "canonical"
_DELTA = "DELTA_BINARY_PACKED"


class PartitionError(RuntimeError):
    """A partition that is missing, does not match its card, or does not fit its month."""


@dataclass(frozen=True)
class PartitionCard:
    table: str
    keys: list[str]
    format: int
    month: str  # YYYY-MM
    part: str
    rows: int
    fingerprint: str  # the content fingerprint (PitTable.fingerprint)
    sha256: str  # of the Parquet file
    single_version: bool
    inputs: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    notes: list[str] = field(default_factory=lambda: list[str]())  # e.g. impossible bars kept

    @property
    def name(self) -> str:
        """How a run's provenance names this partition: its place in the store."""
        return f"{self.table}/v{self.format}/{self.month}/{self.part}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, indent=1) + "\n"

    @classmethod
    def from_json(cls, text: str) -> PartitionCard:
        return cls(**json.loads(text))


def month_dir(table: str, fmt: int, month: date) -> str:
    return f"{ROOT}/{table}/v{fmt}/{month:%Y-%m}"


def _month_bounds(month: date) -> tuple[datetime, datetime]:
    start = datetime(month.year, month.month, 1, tzinfo=UTC)
    nxt = month_start(month, 1)
    return start, datetime(nxt.year, nxt.month, 1, tzinfo=UTC)


class _HashSink(io.RawIOBase):
    """A write-only file that only feeds a SHA-256 (so a large stream is never held in memory)."""

    def __init__(self, prefix: bytes) -> None:
        super().__init__()
        self.hash = hashlib.sha256(prefix)

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:  # noqa: ANN401 - any bytes-like object, as in RawIOBase
        view = memoryview(data)
        self.hash.update(view)
        return view.nbytes


class PartitionWriter:
    """Writes one partition from tables added in key order, holding one row group at a time.

    A month of U1 bars is tens of millions of rows; built in one piece it would
    not fit in a runner's memory. Tables are added one after another (for the
    bars, one symbol at a time, symbols in order) and every ``row_group_rows``
    rows go to the Parquet file and to the fingerprint.

    The partition's fingerprint covers the table name, its keys and the rows in
    canonical order (columns by name; rows by keys and times) cut into batches
    of ``row_group_rows``. It depends only on the content: the same rows give
    the same fingerprint however they were added.
    """

    def __init__(
        self,
        local: Path,
        name: str,
        keys: Sequence[str],
        fmt: int,
        month: date,
        part: str,
        row_group_rows: int,
    ) -> None:
        self.name, self.keys, self.format, self.month, self.part = (
            name,
            tuple(keys),
            fmt,
            month,
            part,
        )
        self._rows_per_group = row_group_rows
        self._bounds = _month_bounds(month)
        self._order = [*self.keys, EVENT_TIME, AVAILABLE_AT, INGESTED_AT]
        self._folder = local / month_dir(name, fmt, month)
        self._pending: list[pa.Table] = []
        self._pending_rows = 0
        self._rows = 0
        self._single = True
        self._last: tuple[Any, ...] | None = None
        self._schema: pa.Schema | None = None
        self._parquet: pq.ParquetWriter | None = None
        self._ipc: Any = None
        self._sink = _HashSink(name.encode() + b"\0" + json.dumps(list(self.keys)).encode() + b"\0")
        self._partial = self._folder / f"{part}.parquet.part"

    def add(self, t: PitTable) -> None:
        """Append ``t``; its rows must all come after the rows added so far, in canonical order."""
        if (t.name, t.keys) != (self.name, self.keys):
            raise PartitionError(f"{t.name}{t.keys} added to a partition of {self.name}{self.keys}")
        if t.num_rows == 0:
            return
        events = t.table.column(EVENT_TIME)
        start, end = self._bounds
        if pc.min(events).as_py() < start or pc.max(events).as_py() >= end:
            raise PartitionError(f"{t.name}: rows outside {self.month:%Y-%m}")
        table = t.canonical()
        if self._schema is None:
            self._open(table.schema)
        elif table.schema != self._schema:
            raise PartitionError(f"{t.name}: the columns differ from the tables added before")
        first = tuple(table.slice(0, 1).select(self._order).to_pylist()[0].values())
        last = tuple(table.slice(table.num_rows - 1, 1).select(self._order).to_pylist()[0].values())
        ids = len(self.keys) + 1  # keys and event_time name one observation
        if self._last is not None:
            if first <= self._last:
                raise PartitionError(f"{t.name}: rows added out of order at {first[:ids]}")
            if first[:ids] == self._last[:ids]:
                self._single = False
        observations = table.group_by(self._order[:ids]).aggregate([([], "count_all")]).num_rows
        self._single = self._single and observations == table.num_rows
        self._last = last
        self._rows += table.num_rows
        self._pending.append(table)
        self._pending_rows += table.num_rows
        while self._pending_rows >= self._rows_per_group:
            self._flush(self._rows_per_group)

    def _open(self, schema: pa.Schema) -> None:
        ints = [
            f.name for f in schema if pa.types.is_int64(f.type) or pa.types.is_timestamp(f.type)
        ]
        others = [f.name for f in schema if f.name not in ints]
        self._folder.mkdir(parents=True, exist_ok=True)
        self._schema = schema
        self._parquet = pq.ParquetWriter(
            self._partial,
            schema,
            compression="zstd",
            # The stubs say bool; pyarrow also takes a list of columns (documented).
            use_dictionary=others,  # pyright: ignore[reportArgumentType]
            column_encoding=dict.fromkeys(ints, _DELTA),
            write_statistics=True,
        )
        self._ipc = ipc.new_stream(pa.PythonFile(self._sink, mode="w"), schema)

    def _flush(self, rows: int) -> None:
        pending = pa.concat_tables(self._pending)
        # A slice keeps its parent's buffers at an offset, and Arrow IPC can then lay
        # the same values out differently. Taking the rows copies them into fresh
        # buffers, so the bytes depend on the values alone.
        chunk = pending.slice(0, rows).take(pa.array(range(rows), type=pa.int64()))
        rest = pending.slice(rows)
        self._pending = [rest] if rest.num_rows else []
        self._pending_rows = rest.num_rows
        if self._parquet is None:
            raise PartitionError("the partition file is not open")
        self._parquet.write_table(chunk, row_group_size=rows)
        self._ipc.write_table(chunk, max_chunksize=rows)

    def close(self, inputs: Mapping[str, str], notes: Sequence[str] = ()) -> PartitionCard:
        """Finish the file and write the card next to it."""
        if self._parquet is None:
            raise PartitionError(f"{self.name} {self.month:%Y-%m}/{self.part}: no rows to write")
        if self._pending_rows:
            self._flush(self._pending_rows)
        self._parquet.close()
        self._ipc.close()
        path = self._folder / f"{self.part}.parquet"
        self._partial.replace(path)
        card = PartitionCard(
            table=self.name,
            keys=list(self.keys),
            format=self.format,
            month=f"{self.month:%Y-%m}",
            part=self.part,
            rows=self._rows,
            fingerprint=str(Fingerprint(self._sink.hash.hexdigest())),
            sha256=digest_file(path).hex,
            single_version=self._single,
            inputs=dict(sorted(inputs.items())),
            notes=list(notes),
        )
        (self._folder / f"{self.part}.json").write_text(card.to_json(), encoding="utf-8")
        return card


def write_partition(
    t: PitTable,
    local: Path,
    fmt: int,
    month: date,
    part: str,
    inputs: Mapping[str, str],
    row_group_rows: int,
) -> PartitionCard:
    """Write a whole table as the partition ``month``/``part`` under ``local``; returns its card."""
    writer = PartitionWriter(local, t.name, t.keys, fmt, month, part, row_group_rows)
    writer.add(t)
    return writer.close(inputs)


@dataclass
class Window:
    """What a query read: the rows, and the fingerprint of every partition behind them."""

    table: pa.Table
    used: dict[str, Fingerprint]


class PartitionedTable:
    """A month-partitioned table in a local directory, filled from the remote store on demand."""

    def __init__(
        self,
        name: str,
        keys: Sequence[str],
        fmt: int,
        local: Path,
        remote: Remote | None = None,
        limits: Limits | None = None,
    ) -> None:
        if (remote is None) != (limits is None):
            raise ValueError("a remote store needs its limits, and limits need a remote store")
        self.name = name
        self.keys = tuple(keys)
        self.format = fmt
        self._local = local
        self._remote = remote
        self._limits = limits
        self._pulled: set[date] = set()

    def _months(self, start: datetime | None, end: datetime) -> list[date]:
        """Months that can hold an event in ``[start, end]``, oldest first."""
        last = month_start(ensure_utc(end).date())
        if start is None:
            stored = self._stored_months()
            return [m for m in stored if m <= last]
        months: list[date] = []
        m = month_start(ensure_utc(start).date())
        while m <= last:
            months.append(m)
            m = month_start(m, 1)
        return months

    def _stored_months(self) -> list[date]:
        prefix = f"{ROOT}/{self.name}/v{self.format}"
        names: set[str] = set()
        if self._remote is not None:
            names |= {p.split("/")[-2] for p in self._remote.list(prefix)}
        base = self._local / prefix
        if base.exists():
            names |= {d.name for d in base.iterdir() if d.is_dir()}
        return sorted(date(int(n[:4]), int(n[5:7]), 1) for n in names)

    def _cards(self, month: date) -> list[tuple[PartitionCard, Path]]:
        folder_name = month_dir(self.name, self.format, month)
        if self._remote is not None and self._limits is not None and month not in self._pulled:
            pull(self._remote, self._local, folder_name, self._limits)
            self._pulled.add(month)
        folder = self._local / folder_name
        if not folder.exists():
            return []
        out: list[tuple[PartitionCard, Path]] = []
        for card_path in sorted(folder.glob("*.json")):
            card = PartitionCard.from_json(card_path.read_text(encoding="utf-8"))
            data = card_path.with_suffix(".parquet")
            if not data.is_file():
                raise PartitionError(f"{card.name}: the card has no data file")
            if digest_file(data).hex != card.sha256:
                raise PartitionError(f"{card.name}: the file does not match its card")
            out.append((card, data))
        return out

    def window(
        self,
        start: datetime | None,
        at: datetime,
        where: Mapping[str, Collection[str]] | None = None,
        columns: Sequence[str] | None = None,
    ) -> Window:
        """Observations with ``event_time`` in ``[start, at]`` as known at ``at``.

        ``where`` keeps only rows whose key columns take the listed values;
        ``columns`` limits the other columns read (keys and times always come).
        """
        at = ensure_utc(at)
        until = pa.scalar(at, type=TIME_TYPE)
        keep = (pc.field(EVENT_TIME) <= until) & (pc.field(AVAILABLE_AT) <= until)
        if start is not None:
            keep = keep & (pc.field(EVENT_TIME) >= pa.scalar(ensure_utc(start), type=TIME_TYPE))
        for key, values in (where or {}).items():
            if key not in self.keys:
                raise KeyError(f"{key!r} is not a key of {self.name} (keys: {self.keys})")
            keep = keep & pc.field(key).isin(sorted(values))
        wanted = None
        if columns is not None:
            wanted = [*self.keys, *TIME_COLUMNS, *(c for c in columns if c not in self.keys)]
        pieces: list[pa.Table] = []
        used: dict[str, Fingerprint] = {}
        single = True
        for month in self._months(start, at):
            for card, path in self._cards(month):
                used[card.name] = Fingerprint.parse(card.fingerprint)
                single = single and card.single_version
                pieces.append(pq.read_table(path, columns=wanted, filters=keep))
        if not pieces:
            fields = [(k, pa.string()) for k in self.keys] + [(c, TIME_TYPE) for c in TIME_COLUMNS]
            return Window(pa.schema(fields).empty_table(), used)
        table = pa.concat_tables(pieces, promote_options="default")
        return Window(latest_versions(table, self.keys, at, single_version=single), used)
