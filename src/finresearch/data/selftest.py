"""Storage self-check: write, read back, compare, and time it (step 2.1 acceptance).

Random bytes of a few sizes are pushed to ``<prefix>/<run id>/`` one file per
commit, pulled into an empty directory and compared by fingerprint. Every call
to the remote store is counted, and so is every transient failure, which gives
the failure rate. The folder is deleted at the end.

Only sizes, timings and counts are printed: the job log of a public repository
is public.
"""

import json
import os
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from finresearch.core.fingerprint import digest_file
from finresearch.data.store import Limits, Remote, TransientStoreError, pull, push

Clock = Callable[[], float]


class CountingRemote:
    """Wraps a remote store and counts calls and transient failures."""

    def __init__(self, inner: Remote) -> None:
        self.inner = inner
        self.calls = 0
        self.transient_failures = 0

    def _count[T](self, fn: Callable[[], T]) -> T:
        self.calls += 1
        try:
            return fn()
        except TransientStoreError:
            self.transient_failures += 1
            raise

    def head(self) -> str:
        return self._count(self.inner.head)

    def list(self, prefix: str) -> dict[str, int]:
        return self._count(lambda: self.inner.list(prefix))

    def read(self, path: str) -> bytes:
        return self._count(lambda: self.inner.read(path))

    def commit(self, files: Mapping[str, Path], message: str, parent: str) -> str:
        return self._count(lambda: self.inner.commit(files, message, parent))

    def remove(self, prefix: str, message: str) -> None:
        self._count(lambda: self.inner.remove(prefix, message))


@dataclass(frozen=True, slots=True)
class SizeResult:
    size_bytes: int
    rounds: int
    verified: int
    upload_seconds_median: float
    download_seconds_median: float
    upload_mb_per_second: float
    download_mb_per_second: float


@dataclass(frozen=True, slots=True)
class SelftestReport:
    sizes: list[SizeResult]
    calls: int
    transient_failures: int

    @property
    def ok(self) -> bool:
        return all(s.verified == s.rounds for s in self.sizes)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "calls": self.calls,
                "transient_failures": self.transient_failures,
                "failure_rate": self.transient_failures / self.calls if self.calls else 0.0,
                "sizes": [asdict(s) for s in self.sizes],
            },
            indent=2,
            sort_keys=True,
        )


_MB = 1_000_000


def _rate(size: int, seconds: float) -> float:
    return size / _MB / seconds if seconds > 0 else 0.0


def selftest(
    remote: Remote,
    work: Path,
    cfg: Mapping[str, Any],
    limits: Limits,
    run_id: str,
    *,
    clock: Clock = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> SelftestReport:
    counting = CountingRemote(remote)
    base = f"{cfg['prefix']}/{run_id}"
    sizes = [int(s) for s in cfg["sizes_bytes"]]
    rounds = int(cfg["rounds"])
    up: dict[int, list[float]] = {s: [] for s in sizes}
    down: dict[int, list[float]] = {s: [] for s in sizes}
    verified = dict.fromkeys(sizes, 0)
    try:
        for r in range(rounds):
            for size in sizes:
                folder = f"{base}/round-{r}/size-{size}"
                source = work / "up" / folder / "blob.bin"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(random_bytes(size))
                t0 = clock()
                push(work / "up", counting, folder, limits, f"selftest {run_id}", sleep)
                t1 = clock()
                pull(counting, work / "down", folder, limits, sleep)
                t2 = clock()
                up[size].append(t1 - t0)
                down[size].append(t2 - t1)
                copy = work / "down" / folder / "blob.bin"
                verified[size] += int(copy.exists() and digest_file(copy) == digest_file(source))
    finally:
        counting.remove(base, f"selftest {run_id}: clean up")
    results = [
        SizeResult(
            size_bytes=s,
            rounds=rounds,
            verified=verified[s],
            upload_seconds_median=statistics.median(up[s]),
            download_seconds_median=statistics.median(down[s]),
            upload_mb_per_second=_rate(s, statistics.median(up[s])),
            download_mb_per_second=_rate(s, statistics.median(down[s])),
        )
        for s in sizes
    ]
    return SelftestReport(results, counting.calls, counting.transient_failures)
