# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Month-partitioned point-in-time tables (Phase 2.5.2): large tables, read a window at a time.

U1's 1-minute bars run to billions of rows, far more than fit in memory. Such a
table is stored as partitions, one per month of ``event_time`` and *part* (for
the bars, the exchange)::

    canonical/<table>/v<format>/<YYYY-MM>/<part>.parquet
    canonical/<table>/v<format>/<YYYY-MM>/<part>.json   the partition's card

The card records the table's keys, the row count, the content fingerprint, the
file's SHA-256, whether every observation has a single version, and the input
files the partition was built from. Partitions are written once (the remote
store is append-only); ``format`` goes up when the table's layout changes, so a
new layout never overwrites an old one.

A ``PartitionedTable`` answers a query from the partitions that overlap its
window only, checks each file against its card before reading it, and pulls
missing partitions from the remote store first. Row-group statistics let a
query for a few symbols skip most of a file.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from finresearch.core.fingerprint import Fingerprint, digest_file
from finresearch.core.time import ensure_utc, month_start
from finresearch.data.store import Limits, Remote, pull
from finresearch.data.table import (
    AVAILABLE_AT,
    EVENT_TIME,
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


def write_partition(
    t: PitTable,
    local: Path,
    fmt: int,
    month: date,
    part: str,
    inputs: Mapping[str, str],
    row_group_rows: int,
) -> PartitionCard:
    """Write ``t`` as the partition ``month``/``part`` under ``local``; returns its card.

    Every row must fall in ``month``. Rows and columns are written in canonical
    order with fixed encodings, so the same table always gives the same file.
    """
    start, end = _month_bounds(month)
    events = t.table.column(EVENT_TIME)
    if t.num_rows and (pc.min(events).as_py() < start or pc.max(events).as_py() >= end):
        raise PartitionError(f"{t.name}: rows outside {month:%Y-%m}")
    table = t.canonical()
    ints = [
        f.name for f in table.schema if pa.types.is_int64(f.type) or pa.types.is_timestamp(f.type)
    ]
    others = [f.name for f in table.schema if f.name not in ints]
    folder = local / month_dir(t.name, fmt, month)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{part}.parquet"
    pq.write_table(
        table,
        path,
        row_group_size=row_group_rows,
        compression="zstd",
        # The stubs say bool; pyarrow also takes a list of columns (documented).
        use_dictionary=others,  # pyright: ignore[reportArgumentType]
        column_encoding=dict.fromkeys(ints, _DELTA),
        write_statistics=True,
    )
    ids = [*t.keys, EVENT_TIME]
    single = (
        t.num_rows == 0 or table.group_by(ids).aggregate([([], "count_all")]).num_rows == t.num_rows
    )
    card = PartitionCard(
        table=t.name,
        keys=list(t.keys),
        format=fmt,
        month=f"{month:%Y-%m}",
        part=part,
        rows=t.num_rows,
        fingerprint=str(t.fingerprint),
        sha256=digest_file(path).hex,
        single_version=single,
        inputs=dict(sorted(inputs.items())),
    )
    (folder / f"{part}.json").write_text(card.to_json(), encoding="utf-8")
    return card


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
