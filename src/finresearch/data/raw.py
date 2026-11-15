"""The raw layer: responses exactly as received, append-only (docs/architecture.md 2.2).

Each response is stored byte-for-byte (gzip with a fixed header, so identical
input gives identical files) next to a metadata file::

    <root>/raw/<source>/<YYYY>/<MM>/<DD>/<name>-<HHMMSS>Z.json.gz
    <root>/raw/<source>/<YYYY>/<MM>/<DD>/<name>-<HHMMSS>Z.meta.json

The metadata records where the bytes came from and their fingerprint. Nothing
is ever overwritten. Year and month folders keep every folder far below the
remote store's limit of 10,000 entries (decision 0003).

A save is all or nothing. Each file is written under a temporary name and
linked into place only when complete, so no half-written file ever has a
final name. If the save fails after the data file is in place, the files it
created are removed again, so a data file without its metadata is never left
behind to be pushed. If a save was cut off between its two files (the process
killed), saving the same bytes again completes it; different bytes are refused.
"""

import gzip
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from finresearch.core.fingerprint import Fingerprint, canonical_json, digest_bytes
from finresearch.core.time import ensure_utc, format_utc, parse_utc


class RawStoreError(RuntimeError):
    """The raw layer would be overwritten, or a file does not match its metadata."""


@dataclass(frozen=True, slots=True)
class RawRecord:
    source: str
    name: str
    url: str
    fetched_at: datetime
    fingerprint: Fingerprint
    path: Path  # relative to the store root

    def meta(self) -> dict[str, str]:
        return {
            "source": self.source,
            "name": self.name,
            "url": self.url,
            "fetched_at": format_utc(self.fetched_at),
            "fingerprint": str(self.fingerprint),
            "path": self.path.as_posix(),
        }


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(
        self, source: str, name: str, url: str, fetched_at: datetime, content: bytes
    ) -> RawRecord:
        fetched_at = ensure_utc(fetched_at)
        day = self.root / "raw" / source / fetched_at.strftime("%Y/%m/%d")
        stem = f"{name}-{fetched_at.strftime('%H%M%S')}Z"
        data_path, meta_path = day / f"{stem}.json.gz", day / f"{stem}.meta.json"
        record = RawRecord(
            source, name, url, fetched_at, digest_bytes(content), data_path.relative_to(self.root)
        )
        meta = canonical_json(record.meta()) + b"\n"
        if meta_path.exists():
            raise RawStoreError(f"{data_path} already exists: the raw layer is never overwritten")
        if data_path.exists():
            # A save cut off between its two files: finish it, but only with the same bytes.
            if gzip.decompress(data_path.read_bytes()) != content:
                raise RawStoreError(
                    f"{data_path} already exists: the raw layer is never overwritten"
                )
            _write_new(meta_path, meta)
            return record
        day.mkdir(parents=True, exist_ok=True)
        _write_new(data_path, _gzip(content, data_path.name))
        try:
            _write_new(meta_path, meta)
        except BaseException:
            data_path.unlink(missing_ok=True)  # created by this call and never seen by anyone
            raise
        return record

    def records(self, source: str) -> list[RawRecord]:
        """Every stored response for ``source``, oldest first."""
        out: list[RawRecord] = []
        for meta_path in sorted((self.root / "raw" / source).rglob("*.meta.json")):
            m = json.loads(meta_path.read_bytes())
            out.append(
                RawRecord(
                    m["source"],
                    m["name"],
                    m["url"],
                    parse_utc(m["fetched_at"]),
                    Fingerprint.parse(m["fingerprint"]),
                    Path(m["path"]),
                )
            )
        return sorted(out, key=lambda r: (r.fetched_at, r.name))

    def read(self, record: RawRecord) -> bytes:
        """The original bytes, after checking them against the recorded fingerprint."""
        content = gzip.decompress((self.root / record.path).read_bytes())
        if digest_bytes(content) != record.fingerprint:
            raise RawStoreError(f"{record.path}: content does not match its recorded fingerprint")
        return content


def _gzip(content: bytes, name: str) -> bytes:
    """``content`` gzipped with a fixed header: no time, ``name`` as the file name."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename=name, mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as gz:
        gz.write(content)
    return buffer.getvalue()


def _write_new(path: Path, content: bytes) -> None:
    """Write ``path``, which must not exist, so that it appears complete or not at all."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    try:
        temporary.write_bytes(content)
        os.link(temporary, path)  # unlike a rename, fails rather than replace an existing file
    except FileExistsError as exc:
        raise RawStoreError(f"{path} already exists: the raw layer is never overwritten") from exc
    finally:
        temporary.unlink(missing_ok=True)
