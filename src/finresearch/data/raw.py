"""The raw layer: responses exactly as received, append-only (docs/architecture.md 2.2).

Each response is stored byte-for-byte (gzip with a fixed header, so identical
input gives identical files) next to a metadata file::

    <root>/raw/<source>/<YYYY>/<MM>/<DD>/<name>-<HHMMSS>Z.json.gz
    <root>/raw/<source>/<YYYY>/<MM>/<DD>/<name>-<HHMMSS>Z.meta.json

The metadata records where the bytes came from and their fingerprint. Nothing
is ever overwritten. Year and month folders keep every folder far below the
remote store's limit of 10,000 entries (decision 0003).
"""

import gzip
import json
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
        if data_path.exists() or meta_path.exists():
            raise RawStoreError(f"{data_path} already exists: the raw layer is never overwritten")
        day.mkdir(parents=True, exist_ok=True)
        with (
            data_path.open("wb") as fh,
            gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz,
        ):
            gz.write(content)
        record = RawRecord(
            source, name, url, fetched_at, digest_bytes(content), data_path.relative_to(self.root)
        )
        meta_path.write_bytes(canonical_json(record.meta()) + b"\n")
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
