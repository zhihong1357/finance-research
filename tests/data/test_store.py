from collections.abc import Mapping
from pathlib import Path

import pytest

from finresearch.data.store import (
    DirectoryRemote,
    Limits,
    StoreError,
    TransientStoreError,
    check_folder_limits,
    local_files,
    pull,
    push,
)

LIMITS = Limits(max_files_per_commit=2, max_entries_per_folder=4, retries=2, retry_wait_seconds=7.0)


def _files(root: Path, names: list[str], prefix: str = "raw/s") -> None:
    for name in names:
        path = root / prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())


class Recorder:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def test_push_adds_missing_files_in_batches(tmp_path: Path) -> None:
    local, remote = tmp_path / "local", DirectoryRemote(tmp_path / "remote")
    _files(local, ["a", "b", "c", "d", "e"])
    roomy = Limits(
        max_files_per_commit=2, max_entries_per_folder=5, retries=0, retry_wait_seconds=0
    )
    report = push(local, remote, "raw/s", roomy, "add five", Recorder())
    assert report.added == [f"raw/s/{n}" for n in "abcde"]
    assert remote.log == ["add five (1/3)", "add five (2/3)", "add five (3/3)"]  # 2 + 2 + 1
    assert remote.read("raw/s/c") == b"c"


def test_pushing_again_is_harmless(tmp_path: Path) -> None:
    local, remote = tmp_path / "local", DirectoryRemote(tmp_path / "remote")
    _files(local, ["a", "b"])
    push(local, remote, "raw/s", LIMITS, "first", Recorder())
    again = push(local, remote, "raw/s", LIMITS, "second", Recorder())
    assert (again.added, again.commits) == ([], [])
    assert again.already_there == ["raw/s/a", "raw/s/b"]
    assert remote.log == ["first"]


def test_nothing_in_the_remote_store_is_replaced(tmp_path: Path) -> None:
    local, remote = tmp_path / "local", DirectoryRemote(tmp_path / "remote")
    _files(local, ["a"])
    push(local, remote, "raw/s", LIMITS, "first", Recorder())
    (local / "raw/s/a").write_bytes(b"A")  # same size, different bytes
    _files(local, ["b"])
    with pytest.raises(StoreError, match="never replaced"):
        push(local, remote, "raw/s", LIMITS, "second", Recorder())
    assert remote.log == ["first"]  # and b was not sent either
    assert remote.read("raw/s/a") == b"a"


class Flaky(DirectoryRemote):
    """Fails the commits whose numbers are listed (1 = the first commit attempted)."""

    def __init__(self, root: Path, fail: set[int]) -> None:
        super().__init__(root)
        self.fail, self.attempts = fail, 0

    def commit(self, files: Mapping[str, Path], message: str, parent: str) -> str:
        self.attempts += 1
        if self.attempts in self.fail:
            raise TransientStoreError("connection reset")
        return super().commit(files, message, parent)


def test_a_failed_batch_is_retried_without_repeating_the_others(tmp_path: Path) -> None:
    local, remote = tmp_path / "local", Flaky(tmp_path / "remote", fail={2})
    _files(local, ["a", "b", "c", "d"])
    sleep = Recorder()
    report = push(local, remote, "raw/s", LIMITS, "add", sleep)
    assert sleep.waits == [7.0]
    assert sorted(report.added) == [f"raw/s/{n}" for n in "abcd"]
    assert len(report.added) == 4
    assert len(remote.log) == 2  # batch 1 before the failure, batch 2 on the retry
    assert local_files(tmp_path / "remote", "") == [f"raw/s/{n}" for n in "abcd"]


def test_retries_run_out(tmp_path: Path) -> None:
    local, remote = tmp_path / "local", Flaky(tmp_path / "remote", fail={1, 2, 3})
    _files(local, ["a"])
    sleep = Recorder()
    with pytest.raises(TransientStoreError):
        push(local, remote, "raw/s", LIMITS, "add", sleep)
    assert sleep.waits == [7.0, 7.0]  # retries = 2: three attempts, two waits


def test_a_concurrent_commit_forces_a_fresh_look(tmp_path: Path) -> None:
    local, remote = tmp_path / "local", DirectoryRemote(tmp_path / "remote")
    _files(local, ["a"])
    stale = remote.head()
    other = tmp_path / "other"
    _files(other, ["z"])
    push(other, remote, "raw/s", LIMITS, "someone else", Recorder())
    with pytest.raises(TransientStoreError, match="no longer the head"):
        remote.commit({"raw/s/a": local / "raw/s/a"}, "late", stale)


def test_folder_limits_count_files_and_subfolders() -> None:
    paths = ["raw/s/2026/01/a", "raw/s/2026/01/b", "raw/s/2026/02/c", "raw/s/2025/12/d"]
    check_folder_limits(paths, "raw/s", 2)  # raw/s: {2025, 2026}; raw/s/2026: {01, 02}; 01: {a, b}
    with pytest.raises(StoreError, match=r"raw/s/2026/01: 3 entries would exceed the limit of 2"):
        check_folder_limits([*paths, "raw/s/2026/01/e"], "raw/s", 2)
    check_folder_limits(["x/1", "x/2", "x/3"], "x", 3)
    with pytest.raises(StoreError, match=r"^/: 3 entries"):
        check_folder_limits(["a/1", "b/1", "c/1"], "", 2)  # the root itself


def test_push_refuses_to_overfill_a_folder(tmp_path: Path) -> None:
    local, remote = tmp_path / "local", DirectoryRemote(tmp_path / "remote")
    _files(local, ["a", "b", "c", "d", "e"])
    with pytest.raises(StoreError, match="would exceed the limit of 4"):
        push(local, remote, "raw/s", LIMITS, "add", Recorder())
    assert remote.log == []


def test_pull_fetches_only_what_is_missing(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    _files(tmp_path / "remote", ["a", "b"])
    local = tmp_path / "local"
    assert pull(remote, local, "raw/s", LIMITS, Recorder()) == ["raw/s/a", "raw/s/b"]
    assert (local / "raw/s/b").read_bytes() == b"b"
    assert pull(remote, local, "raw/s", LIMITS, Recorder()) == []
    assert not list(local.rglob("*.part"))
    (local / "raw/s/a").write_bytes(b"longer")
    with pytest.raises(StoreError, match="local copy differs"):
        pull(remote, local, "raw/s", LIMITS, Recorder())


def test_local_files_are_sorted_and_relative(tmp_path: Path) -> None:
    _files(tmp_path, ["b", "a/c"])
    assert local_files(tmp_path, "raw") == ["raw/s/a/c", "raw/s/b"]
    assert local_files(tmp_path, "missing") == []
