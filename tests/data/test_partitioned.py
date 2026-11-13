import itertools
import tempfile
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from finresearch.core.time import utc
from finresearch.data import bars
from finresearch.data.fixed import parse
from finresearch.data.partitioned import (
    PartitionCard,
    PartitionedTable,
    PartitionError,
    PartitionWriter,
    month_dir,
    write_partition,
)
from finresearch.data.pit import Clock, LookaheadError, PointInTime
from finresearch.data.store import DirectoryRemote, Limits
from finresearch.data.table import PitTable
from tests.data.conftest import ROWS, make_table

SEPTEMBER, OCTOBER = date(2026, 9, 1), date(2026, 10, 1)
ROW_GROUP = 2
LIMITS = Limits(
    max_files_per_commit=50, max_entries_per_folder=9000, retries=0, retry_wait_seconds=0
)


def _split() -> dict[date, PitTable]:
    """The macro table cut by month of event_time."""
    by_month: dict[date, list[tuple[str, datetime, datetime, float]]] = {}
    for r in ROWS:
        by_month.setdefault(date(r[1].year, r[1].month, 1), []).append(r)
    return {m: make_table(rs) for m, rs in by_month.items()}


def _write(root: Path) -> dict[date, PartitionCard]:
    return {
        m: write_partition(t, root, 1, m, "main", {"https://x.invalid/a.zip": "ab" * 32}, ROW_GROUP)
        for m, t in _split().items()
    }


def test_a_partition_has_a_card_that_describes_it(tmp_path: Path) -> None:
    cards = _write(tmp_path)
    sep = cards[SEPTEMBER]
    assert sep.name == "macro/v1/2026-09/main"
    assert (sep.rows, sep.keys, sep.single_version) == (3, ["series"], False)  # CPI was revised
    assert cards[OCTOBER].single_version
    folder = tmp_path / month_dir("macro", 1, SEPTEMBER)
    assert sorted(p.name for p in folder.iterdir()) == ["main.json", "main.parquet"]
    assert PartitionCard.from_json((folder / "main.json").read_text()) == sep


def test_the_same_table_gives_the_same_bytes(tmp_path: Path) -> None:
    a, b = _write(tmp_path / "a"), _write(tmp_path / "b")
    assert a == b
    path = month_dir("macro", 1, SEPTEMBER) + "/main.parquet"
    assert (tmp_path / "a" / path).read_bytes() == (tmp_path / "b" / path).read_bytes()


def test_pieces_added_in_order_give_the_same_partition(tmp_path: Path) -> None:
    whole = _split()[SEPTEMBER]
    one = write_partition(whole, tmp_path / "one", 1, SEPTEMBER, "main", {}, ROW_GROUP)
    rows = whole.canonical()
    writer = PartitionWriter(
        tmp_path / "two", "macro", ("series",), 1, SEPTEMBER, "main", ROW_GROUP
    )
    writer.add(PitTable("macro", ("series",), rows.slice(0, 1)))  # CPI, first version
    writer.add(PitTable("macro", ("series",), rows.slice(1, 2)))  # CPI revised, then PPI
    two = writer.close({})
    assert (two.fingerprint, two.sha256, two.single_version) == (
        one.fingerprint,
        one.sha256,
        False,  # the revision spans the two pieces
    )


def test_pieces_out_of_order_are_refused(tmp_path: Path) -> None:
    rows = _split()[SEPTEMBER].canonical()
    writer = PartitionWriter(tmp_path, "macro", ("series",), 1, SEPTEMBER, "main", ROW_GROUP)
    writer.add(PitTable("macro", ("series",), rows.slice(2, 1)))  # PPI
    with pytest.raises(PartitionError, match="out of order"):
        writer.add(PitTable("macro", ("series",), rows.slice(0, 1)))  # CPI
    with pytest.raises(PartitionError, match="added to a partition of macro"):
        writer.add(make_table(name="other"))
    with pytest.raises(PartitionError, match="no rows"):
        PartitionWriter(tmp_path, "macro", ("series",), 1, OCTOBER, "x", ROW_GROUP).close({})


def test_notes_are_kept_on_the_card(tmp_path: Path) -> None:
    writer = PartitionWriter(tmp_path, "macro", ("series",), 1, OCTOBER, "main", ROW_GROUP)
    writer.add(_split()[OCTOBER])
    card = writer.close({}, ["low above open or close: 1 bar(s)"])
    assert card.notes == ["low above open or close: 1 bar(s)"]
    folder = tmp_path / month_dir("macro", 1, OCTOBER)
    assert PartitionCard.from_json((folder / "main.json").read_text()).notes == card.notes
    assert not list(folder.glob("*.part"))


def test_rows_outside_the_month_are_refused(tmp_path: Path) -> None:
    with pytest.raises(PartitionError, match="outside 2026-10"):
        write_partition(make_table(), tmp_path, 1, OCTOBER, "main", {}, ROW_GROUP)


def test_a_window_reads_only_the_months_it_covers(tmp_path: Path) -> None:
    _write(tmp_path)
    table = PartitionedTable("macro", ("series",), 1, tmp_path)
    w = table.window(utc(2026, 10, 1), utc(2026, 12, 1))
    assert list(w.used) == ["macro/v1/2026-10/main"]
    assert w.table.column("value").to_pylist() == [2.9]


def test_a_window_keeps_the_version_current_at_its_end(tmp_path: Path) -> None:
    _write(tmp_path)
    table = PartitionedTable("macro", ("series",), 1, tmp_path)
    before = table.window(None, utc(2026, 11, 1))
    assert before.table.column("value").to_pylist() == [3.0, 1.0]  # CPI not yet revised
    after = table.window(None, utc(2026, 12, 1))
    assert after.table.column("value").to_pylist() == [3.1, 2.9, 1.0]
    assert sorted(after.used) == ["macro/v1/2026-09/main", "macro/v1/2026-10/main"]


def test_keys_and_columns_narrow_what_is_read(tmp_path: Path) -> None:
    _write(tmp_path)
    table = PartitionedTable("macro", ("series",), 1, tmp_path)
    w = table.window(None, utc(2026, 12, 1), where={"series": {"PPI"}}, columns=[])
    assert w.table.column("series").to_pylist() == ["PPI"]
    assert "value" not in w.table.column_names
    with pytest.raises(KeyError, match="not a key"):
        table.window(None, utc(2026, 12, 1), where={"value": {"1"}})


def test_a_changed_file_is_caught(tmp_path: Path) -> None:
    _write(tmp_path)
    path = tmp_path / month_dir("macro", 1, OCTOBER) / "main.parquet"
    path.write_bytes(path.read_bytes() + b"\0")
    with pytest.raises(PartitionError, match="does not match its card"):
        PartitionedTable("macro", ("series",), 1, tmp_path).window(None, utc(2026, 12, 1))


def test_an_empty_window_still_has_the_key_and_time_columns(tmp_path: Path) -> None:
    w = PartitionedTable("macro", ("series",), 1, tmp_path).window(None, utc(2026, 12, 1))
    assert w.table.num_rows == 0
    assert w.table.column_names == ["series", "event_time", "available_at", "ingested_at"]


def test_missing_months_come_from_the_remote_store(tmp_path: Path) -> None:
    _write(tmp_path / "remote")
    remote = DirectoryRemote(tmp_path / "remote")
    local = tmp_path / "local"
    table = PartitionedTable("macro", ("series",), 1, local, remote, LIMITS)
    w = table.window(utc(2026, 10, 1), utc(2026, 12, 1))
    assert w.table.column("value").to_pylist() == [2.9]
    assert (local / month_dir("macro", 1, OCTOBER)).is_dir()
    assert not (local / month_dir("macro", 1, SEPTEMBER)).exists()  # never needed, never pulled


def test_a_remote_store_needs_its_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs its limits"):
        PartitionedTable("macro", ("series",), 1, tmp_path, DirectoryRemote(tmp_path))


def test_the_door_records_each_partition_it_read(tmp_path: Path) -> None:
    _write(tmp_path)
    table = PartitionedTable("macro", ("series",), 1, tmp_path)
    pit = PointInTime(Clock(limit=utc(2026, 12, 1)), [table])
    snap = pit.window("macro", utc(2026, 10, 1), utc(2026, 11, 30))
    assert snap.column("value") == [2.9]
    assert list(pit.used) == ["macro/v1/2026-10/main"]
    assert pit.latest("macro").num_rows == 3
    assert sorted(pit.used) == ["macro/v1/2026-09/main", "macro/v1/2026-10/main"]
    with pytest.raises(LookaheadError):
        pit.window("macro", utc(2026, 10, 1), utc(2026, 12, 2))


def test_a_view_keeps_partitioned_tables_behind_its_clock(tmp_path: Path) -> None:
    _write(tmp_path)
    pit = PointInTime(
        Clock(limit=utc(2026, 12, 1)), [PartitionedTable("macro", ("series",), 1, tmp_path)]
    )
    view = pit.view(utc(2026, 11, 1))
    assert view.latest("macro").column("value") == [3.0, 1.0]
    with pytest.raises(LookaheadError):
        view.as_of("macro", utc(2026, 11, 2))


def _minutes(n: int, symbol: str) -> PitTable:
    start = int(utc(2026, 10, 1).timestamp()) * 1_000_000
    price = [f"{100 + i % 7}.{i % 10}" for i in range(n)]
    qty = [f"{i % 5}.000" for i in range(n)]
    values = {c: parse(price) for c in bars.PRICES} | {c: parse(qty) for c in bars.QUANTITIES}
    trades = pa.array([i % 5 for i in range(n)], pa.int64())
    starts = pa.array([start + i * 60_000_000 for i in range(n)], pa.int64())
    return bars.bars("binance", symbol, starts, values, trades, utc(2026, 11, 2))


@settings(max_examples=25)
@given(st.lists(st.integers(1, 59), max_size=6, unique=True))
def test_the_fingerprint_does_not_depend_on_how_rows_were_added(cuts: list[int]) -> None:
    whole = pa.concat_tables([_minutes(60, "AAAUSDT").table, _minutes(30, "BBBUSDT").table])
    table = PitTable(bars.NAME, bars.KEYS, whole).canonical()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        one = write_partition(
            PitTable(bars.NAME, bars.KEYS, table), root / "1", 1, OCTOBER, "b", {}, 7
        )
        writer = PartitionWriter(root / "n", bars.NAME, bars.KEYS, 1, OCTOBER, "b", 7)
        edges = [0, *sorted(cuts), table.num_rows]
        for a, b in itertools.pairwise(edges):
            writer.add(PitTable(bars.NAME, bars.KEYS, table.slice(a, b - a)))
        many = writer.close({})
    assert (many.fingerprint, many.sha256, many.rows) == (one.fingerprint, one.sha256, 90)
