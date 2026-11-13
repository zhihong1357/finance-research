"""Listing Binance's public data archive (data.binance.vision) through its bucket index.

The archive is an S3 bucket. Its index answers ``ListObjects`` requests: given a
prefix, it returns up to 1,000 keys (with size, ETag and last-modified time)
and the "folders" one level down. Every zip has a sibling ``.CHECKSUM`` file
holding its SHA-256.

A dataset (``config/archives.toml``) is a folder such as
``data/futures/um/daily/trades/``; under it, one folder per symbol, and for
klines one more folder per interval (``sub = "1m/"``). Listing reads only the
index, never the files.
"""

import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from finresearch.core.time import parse_utc
from finresearch.data.manifest import ManifestEntry
from finresearch.sources import http

INDEX = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FILES = "https://data.binance.vision/"
CHECKSUM_SUFFIX = ".CHECKSUM"
OFFICIAL_CHECKSUMS: bool = True  # every zip has a .CHECKSUM file with its SHA-256
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_PERIOD = re.compile(r"-(\d{4}-\d{2}(?:-\d{2})?)\.zip$")


@dataclass(frozen=True, slots=True)
class Key:
    key: str
    size: int
    etag: str
    last_modified: datetime


@dataclass(frozen=True, slots=True)
class Page:
    folders: list[str]
    keys: list[Key]
    next_marker: str | None  # None on the last page


def parse_page(content: bytes) -> Page:
    """One ``ListObjects`` (version 1) answer."""
    # The payload is AWS S3's own index, not user input, and Python's expat
    # parser does not fetch external entities.
    root = ET.fromstring(content)  # noqa: S314
    folders = [c.findtext(f"{_NS}Prefix") or "" for c in root.iter(f"{_NS}CommonPrefixes")]
    keys = [
        Key(
            c.findtext(f"{_NS}Key") or "",
            int(c.findtext(f"{_NS}Size") or "0"),
            (c.findtext(f"{_NS}ETag") or "").strip('"'),
            parse_utc(c.findtext(f"{_NS}LastModified") or ""),
        )
        for c in root.iter(f"{_NS}Contents")
    ]
    if (root.findtext(f"{_NS}IsTruncated") or "false") != "true":
        return Page(folders, keys, None)
    # S3 sends NextMarker when a delimiter is set; without one the last name is the marker.
    marker = root.findtext(f"{_NS}NextMarker") or max([k.key for k in keys] + folders)
    return Page(folders, keys, marker)


def pages(prefix: str) -> Iterator[Page]:
    """Every page of the index one level under ``prefix``."""
    marker = ""
    while True:
        page = parse_page(
            http.request(
                "GET", INDEX, params={"delimiter": "/", "prefix": prefix, "marker": marker}
            )
        )
        yield page
        if page.next_marker is None:
            return
        marker = page.next_marker


def symbols(spec: Mapping[str, Any]) -> list[str]:
    """The symbol folders under the dataset's root, delisted ones included."""
    root = str(spec["root"])
    return sorted(f.removeprefix(root).strip("/") for page in pages(root) for f in page.folders)


def entries(
    dataset: str, spec: Mapping[str, Any], symbol: str
) -> tuple[list[ManifestEntry], list[str]]:
    """One entry per zip of ``symbol``; also the names that are neither a zip nor its checksum."""
    prefix = f"{spec['root']}{symbol}/{spec.get('sub', '')}"
    keys = [k for page in pages(prefix) for k in page.keys]
    names = {k.key for k in keys}
    found: list[ManifestEntry] = []
    skipped: list[str] = []
    for k in keys:
        m = _PERIOD.search(k.key)
        if m is None:
            if not k.key.endswith(CHECKSUM_SUFFIX):
                skipped.append(k.key)
            continue
        checksum = k.key + CHECKSUM_SUFFIX
        found.append(
            ManifestEntry(
                dataset=dataset,
                symbol=symbol,
                period=m.group(1),
                url=FILES + k.key,
                size=k.size,
                etag=k.etag,
                last_modified=k.last_modified,
                checksum_url=FILES + checksum if checksum in names else None,
            )
        )
    return found, skipped


def describe(entry: ManifestEntry) -> ManifestEntry:
    """The bucket index already gives size, ETag and modification time."""
    return entry


def check(entry: ManifestEntry, content: bytes) -> list[str]:
    """Problems with a downloaded zip: its official SHA-256, its name, its CRCs."""
    problems: list[str] = []
    name = entry.url.rsplit("/", 1)[-1]
    if entry.checksum_url is None:
        problems.append("the archive has no official checksum file for it")
    else:
        fields = http.request("GET", entry.checksum_url).decode("utf-8", errors="replace").split()
        if len(fields) != 2 or fields[1] != name:  # noqa: PLR2004 - "<sha256>  <file name>"
            problems.append(f"the checksum file does not name {name}: {' '.join(fields)[:120]!r}")
        elif fields[0].lower() != hashlib.sha256(content).hexdigest():
            problems.append("SHA-256 of the bytes differs from the official checksum")
    expected = [name.removesuffix(".zip") + ".csv"]
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if z.namelist() != expected:
                problems.append(f"zip holds {z.namelist()[:3]}, expected {expected}")
            bad = z.testzip()
            if bad is not None:
                problems.append(f"zip member {bad} fails its CRC")
    except zipfile.BadZipFile as exc:
        problems.append(f"not a valid zip: {exc}")
    return problems
