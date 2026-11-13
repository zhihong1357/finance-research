"""Archive manifests (Phase 2.4): list an exchange's public archive, keep what changed, spot-check.

``config/archives.toml`` names the datasets. A run for one dataset:

1. pulls the dataset's earlier increments and replays them: what the archive
   held at the last listing;
2. lists the archive now, from its index only, many symbols in parallel;
3. stores the difference as a new increment and pushes it (written once, never
   changed; see ``data/manifest.py``);
4. downloads a few files at random, half of them new ones, and checks them
   against the manifest: size, ETag, the MD5 inside the ETag, the official
   SHA-256 where the archive has one, and what the file holds.

A listing that fails for any symbol fails the whole run and stores nothing: a
partial listing would look like files had been removed. So does a listing with
more names it cannot read than ``max_skipped``: a new naming scheme at the
exchange must stop the run, not quietly leave files out of the manifest.
"""

import hashlib
import random
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc
from finresearch.data.manifest import (
    PREFIX,
    Change,
    ManifestEntry,
    Row,
    diff,
    increment_path,
    read_increment,
    replay,
    write_increment,
)
from finresearch.data.store import Limits, PushReport, Remote, local_files, pull, push
from finresearch.sources import binance_archive, bybit_archive, http


class ArchiveError(RuntimeError):
    """The configuration does not allow the requested run."""


class Lister(Protocol):
    OFFICIAL_CHECKSUMS: bool  # whether every file should have an official checksum file

    def symbols(self, spec: Mapping[str, Any]) -> list[str]: ...

    def entries(
        self, dataset: str, spec: Mapping[str, Any], symbol: str
    ) -> tuple[list[ManifestEntry], list[str]]: ...

    def check(self, entry: ManifestEntry, content: bytes) -> list[str]: ...


LISTERS: dict[str, Lister] = {"binance": binance_archive, "bybit": bybit_archive}


def archive_config() -> dict[str, Any]:
    return load_toml("archives")


def plan(cfg: Mapping[str, Any]) -> list[str]:
    """The enabled datasets, sorted."""
    return sorted(n for n, d in cfg["datasets"].items() if d.get("enabled", False))


def _spec(cfg: Mapping[str, Any], dataset: str) -> Mapping[str, Any]:
    spec = cfg["datasets"].get(dataset)
    if spec is None or not spec.get("enabled", False):
        raise ArchiveError(f"{dataset} is not an enabled dataset in config/archives.toml")
    if spec["exchange"] not in LISTERS:
        raise ArchiveError(f"{dataset}: unknown exchange {spec['exchange']!r}")
    return spec


@dataclass(frozen=True)
class Listing:
    entries: list[ManifestEntry]
    symbols: int
    skipped: list[str]  # names in the archive that are not data files of the dataset


def list_dataset(dataset: str, spec: Mapping[str, Any], workers: int) -> Listing:
    lister = LISTERS[spec["exchange"]]
    symbols = lister.symbols(spec)

    def one(symbol: str) -> tuple[list[ManifestEntry], list[str]]:
        return lister.entries(dataset, spec, symbol)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(one, symbols))
    entries = [e for found, _ in parts for e in found]
    skipped = sorted(name for _, names in parts for name in names)
    return Listing(entries, len(symbols), skipped)


@dataclass(frozen=True)
class SampleCheck:
    url: str
    size: int
    sha256: str
    etag: str
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(entry: ManifestEntry, lister: Lister) -> SampleCheck:
    """Download one file and check it against its manifest entry."""
    reply = http.fetch("GET", entry.url)
    content = reply.content
    etag = reply.headers.get("etag", "").removeprefix("W/").strip('"')
    problems: list[str] = []
    if entry.size is not None and len(content) != entry.size:
        problems.append(f"{len(content)} bytes, the manifest lists {entry.size}")
    if entry.etag is not None and etag != entry.etag:
        problems.append(f"ETag {etag!r}, the manifest lists {entry.etag!r}")
    # A single-part S3 upload's ETag is the MD5 of the bytes; a multipart one has a "-".
    if etag and "-" not in etag and hashlib.md5(content, usedforsecurity=False).hexdigest() != etag:
        problems.append("MD5 of the bytes differs from the ETag")
    problems += lister.check(entry, content)
    return SampleCheck(entry.url, len(content), hashlib.sha256(content).hexdigest(), etag, problems)


def pick(
    entries: Sequence[ManifestEntry],
    new: Sequence[ManifestEntry],
    n: int,
    new_share: float,
    rng: random.Random,
) -> list[ManifestEntry]:
    """``n`` entries at random, about ``new_share`` of them from ``new``; same seed, same pick."""
    fresh = sorted(new, key=lambda e: e.url)
    chosen = rng.sample(fresh, min(len(fresh), round(n * new_share)))
    taken = {e.url for e in chosen}
    rest = sorted((e for e in entries if e.url not in taken), key=lambda e: e.url)
    chosen += rng.sample(rest, min(len(rest), n - len(chosen)))
    return sorted(chosen, key=lambda e: e.url)


@dataclass
class RunResult:
    dataset: str
    increment: str
    symbols: int
    files: int
    bytes_listed: int
    counts: dict[str, int]  # change -> number of rows in the increment
    skipped: list[str]  # all of them; at most ``max_skipped``
    no_checksum: int  # files without an official checksum, where the archive promises one
    pushed: PushReport
    checks: list[SampleCheck] = field(default_factory=lambda: list[SampleCheck]())
    examples: dict[str, list[str]] = field(default_factory=lambda: dict[str, list[str]]())

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def run(
    dataset: str,
    cfg: Mapping[str, Any],
    remote: Remote,
    limits: Limits,
    work: Path,
    now: datetime,
    seed: int,
) -> RunResult:
    """List ``dataset``, push the increment, then spot-check a sample of its files."""
    spec = _spec(cfg, dataset)
    prefix = f"{PREFIX}/{dataset}"
    history = work / "history"
    pull(remote, history, prefix, limits)
    before = replay(read_increment(history / p) for p in local_files(history, prefix))

    listing = list_dataset(dataset, spec, int(cfg["listing"]["workers"]))
    limit = int(cfg["listing"]["max_skipped"])
    if len(listing.skipped) > limit:
        raise ArchiveError(
            f"{dataset}: {len(listing.skipped)} names do not fit the dataset's naming "
            f"(limit {limit}), e.g. {listing.skipped[:3]}; nothing was stored"
        )
    rows = diff(before, listing.entries)
    out = work / "out"
    path = increment_path(dataset, ensure_utc(now))
    write_increment(rows, out / path)
    counts = {str(c): sum(r.change is c for r in rows) for c in Change}
    pushed = push(out, remote, prefix, limits, f"{dataset}: manifest increment {counts}")

    shown = int(cfg["listing"]["max_shown"])
    lister = LISTERS[spec["exchange"]]
    result = RunResult(
        dataset=dataset,
        increment=path,
        symbols=listing.symbols,
        files=len(listing.entries),
        bytes_listed=sum(e.size or 0 for e in listing.entries),
        counts=counts,
        skipped=listing.skipped,
        no_checksum=sum(e.checksum_url is None for e in listing.entries)
        if lister.OFFICIAL_CHECKSUMS
        else 0,
        pushed=pushed,
        examples=_examples(rows, shown),
    )
    n = min(int(spec.get("sample", cfg["verify"]["max_sample"])), int(cfg["verify"]["max_sample"]))
    added = [r.entry for r in rows if r.change is Change.ADDED]
    rng = random.Random(seed)  # noqa: S311 - a reproducible sample, not a secret
    sample = pick(listing.entries, added, n, float(cfg["verify"]["new_share"]), rng)
    result.checks = [verify(e, lister) for e in sample]
    return result


def _examples(rows: list[Row], shown: int) -> dict[str, list[str]]:
    """A few URLs of changed and removed files: an archive that rewrites history is news."""
    out: dict[str, list[str]] = {}
    for change in (Change.CHANGED, Change.REMOVED):
        urls = [r.entry.url for r in rows if r.change is change]
        if urls:
            out[str(change)] = urls[:shown]
    return out
