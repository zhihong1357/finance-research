"""The remote store behind the data layers (decision 0003, docs/architecture.md 2.2 and 7.2).

Jobs work on a local directory. A job pulls the part of the remote store it
needs, works locally, and pushes what it added. Nothing else talks to the
remote store, so the rest of the code only ever sees local files.

The remote store is append-only, like the raw layer: a path, once there, is
never replaced. Pushing a file that is already there with the same bytes does
nothing (so a retried job is harmless); with different bytes it is refused.

Pushes are batched, because Hugging Face recommends 50 to 100 files per commit,
and every commit names the head it was built on. A concurrent writer therefore
makes the commit fail instead of interleaving with it; the push then lists the
remote again and carries on with whatever is still missing.
"""

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from finresearch.core.fingerprint import digest_bytes, digest_file


class StoreError(RuntimeError):
    """A permanent problem: retrying will not help."""


class TransientStoreError(RuntimeError):
    """A network or server problem, or a concurrent commit: worth retrying."""


@dataclass(frozen=True, slots=True)
class Limits:
    max_files_per_commit: int
    max_entries_per_folder: int
    retries: int
    retry_wait_seconds: float

    @classmethod
    def from_config(cls, hub: Mapping[str, Any]) -> Limits:
        return cls(
            int(hub["max_files_per_commit"]),
            int(hub["max_entries_per_folder"]),
            int(hub["retries"]),
            float(hub["retry_wait_seconds"]),
        )


class Remote(Protocol):
    """The few operations a remote store must offer. Paths are POSIX, relative to its root."""

    def head(self) -> str:
        """The current commit."""
        ...

    def list(self, prefix: str) -> dict[str, int]:
        """Every file under ``prefix`` (all of it for ``""``), with its size in bytes."""
        ...

    def read(self, path: str) -> bytes: ...

    def commit(self, files: Mapping[str, Path], message: str, parent: str) -> str:
        """Add ``files`` (remote path -> local file) in one commit on top of ``parent``.

        Raises ``TransientStoreError`` if ``parent`` is no longer the head.
        """
        ...

    def remove(self, prefix: str, message: str) -> None:
        """Delete a folder. Used only to clean up after the self-check."""
        ...


Sleep = Callable[[float], None]


def with_retries[T](fn: Callable[[], T], limits: Limits, sleep: Sleep = time.sleep) -> T:
    for attempt in range(limits.retries + 1):
        try:
            return fn()
        except TransientStoreError:
            if attempt == limits.retries:
                raise
            sleep(limits.retry_wait_seconds)
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class PushReport:
    added: list[str] = field(default_factory=lambda: list[str]())
    already_there: list[str] = field(default_factory=lambda: list[str]())
    commits: list[str] = field(default_factory=lambda: list[str]())


def local_files(local: Path, prefix: str) -> list[str]:
    """Every file under ``local/prefix``, as sorted POSIX paths relative to ``local``."""
    base = local / prefix if prefix else local
    if not base.exists():
        return []
    return sorted(p.relative_to(local).as_posix() for p in base.rglob("*") if p.is_file())


def _folder_entries(paths: Iterable[str], within: str) -> dict[str, set[str]]:
    """Folder -> its immediate children (files and subfolders), at or below ``within``."""
    entries: dict[str, set[str]] = {}
    root = PurePosixPath(within) if within else None
    for path in paths:
        child = PurePosixPath(path)
        for folder in child.parents:
            if root is not None and folder != root and root not in folder.parents:
                break
            key = "" if folder == PurePosixPath() else folder.as_posix()
            entries.setdefault(key, set()).add(child.name)
            child = folder
    return entries


def check_folder_limits(paths: Iterable[str], within: str, limit: int) -> None:
    for folder, children in sorted(_folder_entries(paths, within).items()):
        if len(children) > limit:
            raise StoreError(
                f"{folder or '/'}: {len(children)} entries would exceed the limit of {limit}"
            )


def push(
    local: Path,
    remote: Remote,
    prefix: str,
    limits: Limits,
    message: str,
    sleep: Sleep = time.sleep,
) -> PushReport:
    """Upload every file under ``local/prefix`` that the remote store does not have yet."""
    files = local_files(local, prefix)
    report = PushReport()
    with_retries(
        lambda: _push_once(local, remote, prefix, files, limits, message, report), limits, sleep
    )
    return report


def _push_once(
    local: Path,
    remote: Remote,
    prefix: str,
    files: list[str],
    limits: Limits,
    message: str,
    report: PushReport,
) -> None:
    existing = remote.list(prefix)
    new: list[str] = []
    for path in files:
        if path not in existing:
            new.append(path)
            continue
        if path in report.added or path in report.already_there:
            continue  # committed by an earlier attempt of this push
        same_size = existing[path] == (local / path).stat().st_size
        if not same_size or digest_bytes(remote.read(path)) != digest_file(local / path):
            raise StoreError(f"{path}: the remote store has different bytes; it is never replaced")
        report.already_there.append(path)
    check_folder_limits([*existing, *new], prefix, limits.max_entries_per_folder)
    size = limits.max_files_per_commit
    batches = [new[i : i + size] for i in range(0, len(new), size)]
    for n, batch in enumerate(batches, start=1):
        label = f"{message} ({n}/{len(batches)})" if len(batches) > 1 else message
        oid = remote.commit({p: local / p for p in batch}, label, remote.head())
        report.commits.append(oid)
        report.added.extend(batch)


def pull(
    remote: Remote, local: Path, prefix: str, limits: Limits, sleep: Sleep = time.sleep
) -> list[str]:
    """Download every file under ``prefix`` that ``local`` does not have yet; returns them."""
    listing = with_retries(lambda: remote.list(prefix), limits, sleep)
    fetched: list[str] = []
    for path, size in sorted(listing.items()):
        target = local / path
        if target.exists():
            if target.stat().st_size != size:
                raise StoreError(f"{path}: the local copy differs from the remote store")
            continue
        data = with_retries(lambda p=path: remote.read(p), limits, sleep)
        if len(data) != size:
            raise StoreError(f"{path}: read {len(data)} bytes, the remote store lists {size}")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.name}.part")
        partial.write_bytes(data)
        partial.replace(target)  # never leave a half-written file under the final name
        fetched.append(path)
    return fetched


class DirectoryRemote:
    """A remote store kept in a local directory: for tests, and for trying things out offline."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.log: list[str] = []  # commit messages, oldest first

    def head(self) -> str:
        return f"commit-{len(self.log)}"

    def list(self, prefix: str) -> dict[str, int]:
        return {p: (self.root / p).stat().st_size for p in local_files(self.root, prefix)}

    def read(self, path: str) -> bytes:
        target = self.root / path
        if not target.is_file():
            raise StoreError(f"{path}: not in the remote store")
        return target.read_bytes()

    def commit(self, files: Mapping[str, Path], message: str, parent: str) -> str:
        if parent != self.head():
            raise TransientStoreError(f"{parent} is no longer the head")
        clash = sorted(p for p in files if (self.root / p).exists())
        if clash:
            raise StoreError(f"{clash}: already in the remote store")
        for path, source in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        self.log.append(message)
        return self.head()

    def remove(self, prefix: str, message: str) -> None:
        for path in local_files(self.root, prefix):
            (self.root / path).unlink()
        self.log.append(message)
