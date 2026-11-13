import gzip
from pathlib import Path

import pytest

from finresearch.core.fingerprint import digest_bytes
from finresearch.core.time import utc
from finresearch.data.raw import RawStore, RawStoreError

AT = utc(2026, 11, 13, 4, 5, 6)


def test_saved_bytes_come_back_unchanged(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    rec = store.save("src.a", "series", "https://example.org/x", AT, b'{"v": 1}')
    assert rec.path == Path("raw/src.a/2026-11-13/series-040506Z.json.gz")
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
