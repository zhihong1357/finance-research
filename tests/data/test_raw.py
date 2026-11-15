import gzip
from pathlib import Path

import pytest

from finresearch.core.fingerprint import digest_bytes
from finresearch.core.time import utc
from finresearch.data import raw
from finresearch.data.raw import RawStore, RawStoreError

AT = utc(2026, 11, 13, 4, 5, 6)


def test_saved_bytes_come_back_unchanged(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    rec = store.save("src.a", "series", "https://example.org/x", AT, b'{"v": 1}')
    assert rec.path == Path("raw/src.a/2026/11/13/series-040506Z.json.gz")
    assert rec.fingerprint == digest_bytes(b'{"v": 1}')
    assert store.records("src.a") == [rec]
    assert store.read(rec) == b'{"v": 1}'


def test_identical_input_gives_identical_files(tmp_path: Path) -> None:
    a = RawStore(tmp_path / "a").save("s.x", "n", "u", AT, b"payload")
    b = RawStore(tmp_path / "b").save("s.x", "n", "u", AT, b"payload")
    assert (tmp_path / "a" / a.path).read_bytes() == (tmp_path / "b" / b.path).read_bytes()


def test_nothing_is_ever_overwritten(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store.save("s.x", "n", "u", AT, b"first")
    with pytest.raises(RawStoreError, match="never overwritten"):
        store.save("s.x", "n", "u", AT, b"second")
    assert store.read(store.records("s.x")[0]) == b"first"


def test_tampering_is_detected(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    rec = store.save("s.x", "n", "u", AT, b"original")
    (tmp_path / rec.path).write_bytes(gzip.compress(b"altered"))
    with pytest.raises(RawStoreError, match="does not match"):
        store.read(rec)


def test_records_are_oldest_first(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    late = store.save("s.x", "n", "u", utc(2026, 11, 14), b"2")
    early = store.save("s.x", "n", "u", utc(2026, 11, 13), b"1")
    assert store.records("s.x") == [early, late]
    assert store.records("other.source") == []


def test_the_file_format_is_unchanged(tmp_path: Path) -> None:
    # Byte for byte what the store wrote before saves became all or nothing: a gzip
    # header with no time and the file's own name.
    rec = RawStore(tmp_path).save("s.x", "n", "u", AT, b"payload")
    path = tmp_path / rec.path
    with path.open("rb") as fh, gzip.GzipFile(fileobj=fh, mode="rb") as gz:
        assert gz.read() == b"payload"
    header = path.read_bytes()[:10]
    assert header[:4] == b"\x1f\x8b\x08\x08"  # gzip, deflate, a file name follows
    assert header[4:8] == b"\x00\x00\x00\x00"  # no time
    assert path.read_bytes()[10:].startswith(b"n-040506Z.json\x00")


def test_a_failed_save_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    write = raw._write_new  # pyright: ignore[reportPrivateUsage]

    def failing(path: Path, content: bytes) -> None:
        calls.append(path)
        if path.name.endswith(".meta.json"):
            raise OSError("disk full")
        write(path, content)

    monkeypatch.setattr(raw, "_write_new", failing)
    with pytest.raises(OSError, match="disk full"):
        RawStore(tmp_path).save("s.x", "n", "u", AT, b"payload")
    assert len(calls) == 2  # the data file was in place before the metadata failed
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_save_cut_off_between_its_files_is_completed_by_the_same_bytes(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    rec = store.save("s.x", "n", "u", AT, b"payload")
    (tmp_path / rec.path).with_name("n-040506Z.meta.json").unlink()  # as if killed before it
    assert store.records("s.x") == []
    assert store.save("s.x", "n", "u", AT, b"payload") == rec
    assert store.records("s.x") == [rec]
    (tmp_path / rec.path).with_name("n-040506Z.meta.json").unlink()
    with pytest.raises(RawStoreError, match="never overwritten"):
        store.save("s.x", "n", "u", AT, b"other bytes")
    assert gzip.decompress((tmp_path / rec.path).read_bytes()) == b"payload"


def test_no_temporary_file_is_left(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store.save("s.x", "n", "u", AT, b"payload")
    with pytest.raises(RawStoreError):
        store.save("s.x", "n", "u", AT, b"payload")
    names = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
    assert names == ["n-040506Z.json.gz", "n-040506Z.meta.json"]
