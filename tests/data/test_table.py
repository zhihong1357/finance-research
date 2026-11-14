from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finresearch.core.time import utc
from finresearch.data.table import (
    TIME_TYPE,
    PitTable,
    TableError,
    events_between,
    from_parquet,
    to_parquet,
    versions_as_of,
)
from tests.data.conftest import ROWS, make_table


def _values(t: pa.Table) -> list[tuple[object, object, object]]:
    return list(
        zip(
            t.column("series").to_pylist(),
            t.column("event_time").to_pylist(),
            t.column("value").to_pylist(),
            strict=True,
        )
    )


def test_versions_as_of_follow_publication_and_revision(macro: PitTable) -> None:
    assert (
        versions_as_of(macro, utc(2026, 10, 15, 12, 29, 59)).num_rows == 0
    )  # nothing published yet
    assert _values(versions_as_of(macro, utc(2026, 10, 15, 12, 30))) == [
        ("CPI", utc(2026, 9, 30), 3.0)
    ]  # inclusive
    assert _values(versions_as_of(macro, utc(2026, 11, 1))) == [
        ("CPI", utc(2026, 9, 30), 3.0),
        ("PPI", utc(2026, 9, 30), 1.0),
    ]
    assert _values(versions_as_of(macro, utc(2026, 11, 13, 13, 30))) == [
        ("CPI", utc(2026, 9, 30), 3.1),  # the revision replaces the first release
        ("CPI", utc(2026, 10, 31), 2.9),
        ("PPI", utc(2026, 9, 30), 1.0),
    ]


def test_returned_rows_keep_their_columns_and_utc(macro: PitTable) -> None:
    out = versions_as_of(macro, utc(2026, 12, 1))
    assert out.column_names == macro.table.column_names
    assert out.schema.field("available_at").type == TIME_TYPE


def test_fingerprint_ignores_row_order_and_chunking(macro: PitTable) -> None:
    shuffled = make_table([ROWS[2], ROWS[0], ROWS[3], ROWS[1]])
    chunked = PitTable(
        "macro", ("series",), pa.concat_tables([macro.table.slice(0, 2), macro.table.slice(2)])
    )
    assert shuffled.fingerprint == macro.fingerprint == chunked.fingerprint


def test_fingerprint_sees_content_name_and_keys(macro: PitTable) -> None:
    changed = make_table([*ROWS[:3], ("PPI", utc(2026, 9, 30), utc(2026, 10, 16, 12, 30), 1.1)])
    assert changed.fingerprint != macro.fingerprint
    assert make_table(name="other").fingerprint != macro.fingerprint


def test_fingerprint_format_anchor(macro: PitTable) -> None:
    # Pins the logical-content fingerprint (Arrow IPC of the canonical table). A change here
    # silently changes every recorded data fingerprint: it needs a deliberate migration.
    assert (
        str(macro.fingerprint)
        == "sha256:fc8c21c8b113cca1737953f7c832303a0d4a8d372afb3d14ddb619bc10d5f7cb"
    )


def test_parquet_round_trip(tmp_path: Path, macro: PitTable) -> None:
    path = tmp_path / "macro.parquet"
    to_parquet(macro, path)
    again = from_parquet(path)
    assert (again.name, again.keys) == ("macro", ("series",))
    assert again.fingerprint == macro.fingerprint
    pq.write_table(macro.table, tmp_path / "plain.parquet")
    with pytest.raises(TableError, match="no point-in-time metadata"):
        from_parquet(tmp_path / "plain.parquet")


def _with(macro: PitTable, column: str, array: pa.Array | pa.ChunkedArray) -> pa.Table:
    i = macro.table.column_names.index(column)
    return macro.table.set_column(i, column, array)


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda m: m.table.drop_columns(["ingested_at"]), "missing column 'ingested_at'"),
        (
            lambda m: _with(
                m, "available_at", m.table.column("available_at").cast(pa.timestamp("us"))
            ),
            "must be",
        ),
        (
            lambda m: _with(
                m, "available_at", m.table.column("available_at").cast(pa.timestamp("ms", tz="UTC"))
            ),
            "must be",
        ),
        (lambda m: _with(m, "series", pa.array(["CPI", None, "CPI", "PPI"])), "has nulls"),
        (lambda m: m.table.take([0, 0]), "share keys"),
        (
            lambda m: _with(
                m, "ingested_at", m.table.column("available_at").cast(TIME_TYPE).take([0, 0, 0, 0])
            ),
            "ingested before it was available",
        ),
    ],
)
def test_contract_violations(
    macro: PitTable, make: Callable[[PitTable], pa.Table], message: str
) -> None:
    with pytest.raises(TableError, match=message):
        PitTable("macro", ("series",), make(macro))


def test_time_columns_cannot_be_keys(macro: PitTable) -> None:
    with pytest.raises(TableError, match="cannot be keys"):
        PitTable("macro", ("series", "event_time"), macro.table)


def test_events_between_keeps_every_version_inside_the_interval(macro: PitTable) -> None:
    # September's two versions (30 September, first print and revision) and PPI's; the end
    # of the interval is excluded, so October's observation of 31 October is left out.
    september = events_between(macro, utc(2026, 9, 1), utc(2026, 10, 31))
    assert set(september.table.column("value").to_pylist()) == {1.0, 3.0, 3.1}
    assert september.num_rows == 3
    assert events_between(macro, utc(2026, 10, 31), utc(2026, 11, 1)).num_rows == 1
    assert events_between(macro, utc(2026, 11, 1), utc(2026, 12, 1)).num_rows == 0
