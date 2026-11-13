import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finresearch.core.fingerprint import (
    Fingerprint,
    FingerprintError,
    canonical_json,
    digest_bytes,
    digest_file,
    digest_tree,
    digest_value,
    tree_listing,
)
from finresearch.core.time import NaiveDatetimeError, utc

# Published SHA-256 test vectors (FIPS 180-2 "abc"; the empty message)
EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_known_vectors() -> None:
    assert digest_bytes(b"").hex == EMPTY
    assert digest_bytes(b"abc").hex == ABC
    assert str(digest_bytes(b"abc")) == f"sha256:{ABC}"


def test_fingerprint_text_form() -> None:
    assert Fingerprint.parse(f"sha256:{ABC}") == Fingerprint(ABC)
    for bad in (ABC, f"md5:{ABC}", "sha256:XYZ", f"sha256:{ABC.upper()}"):
        with pytest.raises(FingerprintError):
            Fingerprint.parse(bad)


def test_digest_file_streams(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    assert digest_file(f).hex == ABC
    assert digest_file(f, chunk_size=1).hex == ABC  # several reads give the same digest


def _make_tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return root


def test_tree_listing_is_hand_checkable(tmp_path: Path) -> None:
    root = _make_tree(tmp_path / "t", {"sub/b.txt": b"", "a.txt": b"abc"})
    (root / "empty_dir").mkdir()
    expected = f"a.txt\0{ABC}\nsub/b.txt\0{EMPTY}\n".encode()
    assert tree_listing(root) == expected
    assert digest_tree(root).hex == hashlib.sha256(expected).hexdigest()


def test_tree_digest_ignores_location_and_creation_order(tmp_path: Path) -> None:
    one = _make_tree(tmp_path / "one", {"a.txt": b"abc", "z/y.txt": b"1"})
    two = _make_tree(tmp_path / "elsewhere" / "two", {"z/y.txt": b"1", "a.txt": b"abc"})
    assert digest_tree(one) == digest_tree(two)


@pytest.mark.parametrize(
    "change",
    [
        {"a.txt": b"abd"},  # content
        {"b.txt": b"abc", "z/y.txt": b"1"},  # rename
        {"a.txt": b"abc", "z/y.txt": b"1", "new.txt": b""},  # addition
    ],
)
def test_tree_digest_detects_changes(tmp_path: Path, change: dict[str, bytes]) -> None:
    base = {"a.txt": b"abc", "z/y.txt": b"1"}
    changed = change if "z/y.txt" in change else {**base, **change}
    assert digest_tree(_make_tree(tmp_path / "base", base)) != digest_tree(
        _make_tree(tmp_path / "chg", changed)
    )


def test_tree_refuses_symlinks_and_files(tmp_path: Path) -> None:
    root = _make_tree(tmp_path / "t", {"a.txt": b"abc"})
    (root / "link").symlink_to(root / "a.txt")
    with pytest.raises(FingerprintError, match="symbolic"):
        digest_tree(root)
    with pytest.raises(FingerprintError, match="not a directory"):
        digest_tree(root / "a.txt")


class Colour(StrEnum):
    RED = "red"


def test_canonical_json_format_anchor() -> None:
    value = {
        "b": 1,
        "a": [1.5, "研究", None, True, 1.0],
        "when": utc(2026, 11, 13, 3, 15, 13),
        "day": date(2026, 11, 13),
        "colour": Colour.RED,
        "pair": (1, 2),
    }
    assert (
        canonical_json(value)
        == (
            '{"a":[1.5,"研究",null,true,1.0],"b":1,"colour":"red","day":"2026-11-13",'
            '"pair":[1,2],"when":"2026-11-13T03:15:13.000000Z"}'
        ).encode()
    )


def test_canonical_json_distinguishes_what_differs() -> None:
    assert canonical_json(1) != canonical_json(1.0)
    assert canonical_json(True) != canonical_json(1)
    assert canonical_json("1") != canonical_json(1)
    assert digest_value({"x": 1, "y": 2}) == digest_value({"y": 2, "x": 1})


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (float("nan"), FingerprintError),
        (float("inf"), FingerprintError),
        ({1, 2}, FingerprintError),  # sets have no order
        ({1: "a"}, FingerprintError),  # non-string key
        (b"raw", FingerprintError),
        (object(), FingerprintError),
        (datetime(2026, 11, 13), NaiveDatetimeError),  # noqa: DTZ001 - the point of the test
    ],
)
def test_ambiguous_values_are_refused(value: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        canonical_json({"k": value})


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4)
    ),
    max_leaves=20,
)


@given(json_values)
def test_canonical_json_is_a_fixed_point(value: object) -> None:
    once = canonical_json(value)
    assert canonical_json(json.loads(once)) == once


@given(st.dictionaries(st.text(), st.integers(), min_size=2, max_size=8))
def test_key_order_never_matters(d: dict[str, int]) -> None:
    assert canonical_json(d) == canonical_json(dict(reversed(list(d.items()))))
