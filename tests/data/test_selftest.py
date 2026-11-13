from collections.abc import Callable, Mapping
from itertools import count
from pathlib import Path

from finresearch.data.selftest import selftest
from finresearch.data.store import DirectoryRemote, Limits, TransientStoreError

LIMITS = Limits(
    max_files_per_commit=50, max_entries_per_folder=9000, retries=2, retry_wait_seconds=0
)
CFG = {"prefix": "_selftest", "sizes_bytes": [10, 1000], "rounds": 2}


def ticking() -> Callable[[], float]:
    ticks = count()
    return lambda: float(next(ticks))  # every call to the clock advances one second


def test_a_clean_round_trip(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    report = selftest(remote, tmp_path / "work", CFG, LIMITS, "r1", clock=ticking())
    assert report.ok
    assert [(s.size_bytes, s.rounds, s.verified) for s in report.sizes] == [
        (10, 2, 2),
        (1000, 2, 2),
    ]
    # clock: push starts at t, ends t+1, pull ends t+2 -> one second each way
    assert {(s.upload_seconds_median, s.download_seconds_median) for s in report.sizes} == {
        (1.0, 1.0)
    }
    assert report.sizes[1].upload_mb_per_second == 1000 / 1_000_000
    assert report.transient_failures == 0
    assert remote.list("_selftest") == {}  # cleaned up
    assert remote.log[-1] == "selftest r1: clean up"


class FailsFirstCommit(DirectoryRemote):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.failed = False

    def commit(self, files: Mapping[str, Path], message: str, parent: str) -> str:
        if not self.failed:
            self.failed = True
            raise TransientStoreError("reset")
        return super().commit(files, message, parent)


def test_transient_failures_are_counted(tmp_path: Path) -> None:
    report = selftest(FailsFirstCommit(tmp_path / "remote"), tmp_path / "work", CFG, LIMITS, "r2")
    assert report.ok
    assert report.transient_failures == 1
    assert '"failure_rate"' in report.to_json()
