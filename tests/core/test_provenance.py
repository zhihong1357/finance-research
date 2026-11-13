import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from finresearch.core.fingerprint import Fingerprint, digest_bytes, digest_value
from finresearch.core.provenance import (
    MANIFEST_VERSION,
    CodeVersion,
    ProvenanceError,
    RunManifest,
    code_version,
    environment_fingerprint,
)
from finresearch.core.time import utc

GIT = shutil.which("git") or "git"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(  # noqa: S603 - test helper with fixed arguments
        [GIT, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "first")
    return r


def test_code_version_of_a_clean_tree(repo: Path) -> None:
    v = code_version(repo)
    assert v == CodeVersion(_git(repo, "rev-parse", "HEAD"), dirty=False)


def test_local_changes_make_the_tree_dirty(repo: Path) -> None:
    (repo / "a.txt").write_text("two\n")
    assert code_version(repo).dirty
    _git(repo, "checkout", "-q", "--", "a.txt")
    (repo / "untracked.txt").write_text("x\n")
    assert code_version(repo).dirty


def test_outside_a_repository(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceError, match="rev-parse"):
        code_version(tmp_path)


def test_commit_must_be_a_full_id() -> None:
    with pytest.raises(ProvenanceError):
        CodeVersion("0123abc", dirty=False)


def test_environment_fingerprint(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n")
    base = environment_fingerprint(lock, python="3.14.6", machine="x86_64", system="Linux")
    assert base == environment_fingerprint(lock, python="3.14.6", machine="x86_64", system="Linux")
    assert base != environment_fingerprint(lock, python="3.14.7", machine="x86_64", system="Linux")
    assert base != environment_fingerprint(lock, python="3.14.6", machine="arm64", system="Linux")
    lock.write_text("version = 2\n")
    assert base != environment_fingerprint(lock, python="3.14.6", machine="x86_64", system="Linux")


def _manifest() -> RunManifest:
    return RunManifest(
        code=CodeVersion(COMMIT, dirty=False),
        environment=digest_bytes(b"environment"),
        config=digest_value({"lookback": 20, "threshold": 3.0}),
        data={"trades": digest_bytes(b"trades"), "funding": digest_bytes(b"funding")},
        seed=7,
        clock_limit=utc(2027, 6, 30, 23, 59, 59),
    )


def test_manifest_format_anchor() -> None:
    # Pins the serialisation. If this changes, every recorded result fingerprint changes with it:
    # that needs a new MANIFEST_VERSION, not an edit to this number.
    assert MANIFEST_VERSION == 1
    assert _manifest().fingerprint == Fingerprint(
        "08298460e4e9469e8e436c30390a3fb23aa9b7416de3ea4df299147d66015a2a"
    )


def test_data_order_does_not_matter() -> None:
    m = _manifest()
    reordered = dataclasses.replace(m, data=dict(reversed(list(m.data.items()))))
    assert reordered.fingerprint == m.fingerprint
    assert list(reordered.data) == ["funding", "trades"]


@pytest.mark.parametrize(
    "change",
    [
        {"code": CodeVersion(COMMIT, dirty=True)},
        {"code": CodeVersion("f" * 40, dirty=False)},
        {"environment": digest_bytes(b"other environment")},
        {"config": digest_value({"lookback": 21, "threshold": 3.0})},
        {"data": {"trades": digest_bytes(b"trades v2"), "funding": digest_bytes(b"funding")}},
        {"data": {"trades": digest_bytes(b"trades")}},
        {"seed": 8},
        {"clock_limit": utc(2027, 7, 1)},
        {"clock_limit": None},
    ],
)
def test_every_field_changes_the_fingerprint(change: dict[str, object]) -> None:
    m = _manifest()
    assert dataclasses.replace(m, **change).fingerprint != m.fingerprint


def test_round_trip() -> None:
    m = _manifest()
    again = RunManifest.from_dict(m.to_dict())
    assert again == m
    assert again.fingerprint == m.fingerprint


def test_reproducibility_flag_and_validation() -> None:
    assert _manifest().reproducible
    assert not dataclasses.replace(_manifest(), code=CodeVersion(COMMIT, dirty=True)).reproducible
    with pytest.raises(ProvenanceError, match="seed"):
        dataclasses.replace(_manifest(), seed=-1)
    with pytest.raises(ProvenanceError, match="version"):
        RunManifest.from_dict({**_manifest().to_dict(), "manifest_version": 2})


def test_this_repository_has_provenance() -> None:
    root = Path(__file__).resolve().parents[2]
    assert len(code_version(root).commit) == 40
    assert environment_fingerprint(root / "uv.lock").hex
