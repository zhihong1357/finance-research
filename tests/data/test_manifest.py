from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from finresearch.core.time import utc
from finresearch.data.manifest import (
    Change,
    ManifestEntry,
    Row,
    diff,
    increment_path,
    increment_time,
    read_increment,
    replay,
    write_increment,
)


def _e(name: str, size: int | None = 10, etag: str | None = "e1") -> ManifestEntry:
    return ManifestEntry(
        "d", "AAAUSDT", "2026-11-10", f"https://x.invalid/{name}.zip", size, etag,
        utc(2026, 11, 12, 1, 2, 3) if size is not None else None,
        f"https://x.invalid/{name}.zip.CHECKSUM" if size is not None else None,
    )  # fmt: skip


def test_the_increment_path_names_the_listing_time() -> None:
    assert (
        increment_path("binance.um.metrics.daily", utc(2026, 11, 15, 3, 37, 5))
        == "manifests/binance.um.metrics.daily/20261115T033705Z.parquet"
    )


def test_the_listing_time_comes_back_from_the_path() -> None:
    assert increment_time("manifests/d/20261115T033705Z.parquet") == utc(2026, 11, 15, 3, 37, 5)
    assert increment_time("manifests/d/notes.txt") is None


def test_diff_keeps_only_what_changed() -> None:
    before = {e.url: e for e in [_e("a"), _e("b"), _e("c")]}
    listing = [_e("a"), _e("b", etag="e2"), _e("d")]
    assert diff(before, listing) == [
        Row(Change.CHANGED, _e("b", etag="e2")),
        Row(Change.REMOVED, _e("c")),
        Row(Change.ADDED, _e("d")),
    ]


def test_an_unchanged_listing_gives_an_empty_increment() -> None:
    assert diff({e.url: e for e in [_e("a")]}, [_e("a")]) == []


def test_increments_round_trip_with_empty_fields(tmp_path: Path) -> None:
    rows = [Row(Change.ADDED, _e("b", size=None, etag=None)), Row(Change.REMOVED, _e("a"))]
    write_increment(rows, tmp_path / "m" / "x.parquet")
    assert read_increment(tmp_path / "m" / "x.parquet") == sorted(rows, key=lambda r: r.entry.url)


def test_an_empty_increment_round_trips(tmp_path: Path) -> None:
    write_increment([], tmp_path / "x.parquet")
    assert read_increment(tmp_path / "x.parquet") == []


def test_the_same_rows_in_any_order_give_the_same_bytes(tmp_path: Path) -> None:
    rows = [Row(Change.ADDED, _e(n)) for n in "cab"]
    write_increment(rows, tmp_path / "1.parquet")
    write_increment(rows[::-1], tmp_path / "2.parquet")
    assert (tmp_path / "1.parquet").read_bytes() == (tmp_path / "2.parquet").read_bytes()


_listing = st.dictionaries(
    st.sampled_from("abcdef"), st.tuples(st.integers(1, 3), st.sampled_from(["e1", "e2"]))
).map(lambda d: [_e(n, size, etag) for n, (size, etag) in sorted(d.items())])


@given(st.lists(_listing, min_size=1, max_size=5))
def test_replaying_the_increments_gives_back_each_listing(
    listings: list[list[ManifestEntry]],
) -> None:
    increments: list[list[Row]] = []
    state: dict[str, ManifestEntry] = {}
    for listing in listings:
        increments.append(diff(state, listing))
        state = replay(increments)
        assert state == {e.url: e for e in listing}
