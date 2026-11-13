"""Listing Bybit's public archive (public.bybit.com).

The archive is a plain directory listing: under ``trading/``, one folder per
contract and one gzip CSV of trades per day, named ``<SYMBOL><YYYY-MM-DD>.csv.gz``.
The listing gives names only. There is no checksum file, so size, ETag and
SHA-256 are taken when a file is first downloaded (docs/architecture.md 7.3).

The folder is not always the symbol in the file name: when Bybit reused a
symbol, the old contract's files moved to a renamed folder (``ANTOLDUSDT``
holds ``ANTUSDT2021-12-17.csv.gz``), and a few days were reissued as
``..._v2.csv.gz``. Symbols of dated contracts carry hyphens and underscores
(``BTC-01DEC23``, ``WC_..._USDT-28JUN26``), so a symbol is whatever comes before
the date. A manifest entry's ``symbol`` is the folder, which keeps the
two contracts apart; the traded symbol is the one in the file name.
"""

import csv
import gzip
import io
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from finresearch.data.manifest import ManifestEntry
from finresearch.sources import http

BASE = "https://public.bybit.com/"
OFFICIAL_CHECKSUMS: bool = False  # only the ETag, which is the MD5 for single-part uploads
_LINK = re.compile(r'<a href="([^"?#]+)">')
_FILE = re.compile(r"(?P<symbol>[^/]+?)(?P<day>\d{4}-\d{2}-\d{2})(?:_v\d+)?\.csv\.gz")


def _links(url: str) -> list[str]:
    return _LINK.findall(http.request("GET", url).decode("utf-8", errors="replace"))


def symbols(spec: Mapping[str, Any]) -> list[str]:
    return sorted(
        link.rstrip("/") for link in _links(BASE + str(spec["root"])) if link.endswith("/")
    )


def entries(
    dataset: str, spec: Mapping[str, Any], symbol: str
) -> tuple[list[ManifestEntry], list[str]]:
    """One entry per daily file in the folder ``symbol``; also the names that do not fit."""
    folder = f"{BASE}{spec['root']}{symbol}/"
    found: list[ManifestEntry] = []
    skipped: list[str] = []
    for link in _links(folder):
        m = _FILE.fullmatch(link)
        if m is None:
            skipped.append(folder + link)
        else:
            found.append(ManifestEntry(dataset, symbol, m["day"], folder + link))
    return found, skipped


def check(entry: ManifestEntry, content: bytes) -> list[str]:
    """Problems with a downloaded day file: it must be a gzip CSV of the trades of the
    symbol in its name on that UTC day (there is no checksum file, so the content is the check)."""
    m = _FILE.fullmatch(entry.url.rsplit("/", 1)[-1])
    if m is None:
        return [f"not a day file name: {entry.url}"]
    traded = m["symbol"]
    start = datetime.fromisoformat(entry.period).replace(tzinfo=UTC)
    first, last = start.timestamp(), (start + timedelta(days=1)).timestamp()
    rows = wrong_symbol = outside = 0
    try:
        with gzip.open(io.BytesIO(content), "rt", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, list[str]())
            if "timestamp" not in header or "symbol" not in header:
                return [f"unexpected header {header[:4]}"]
            ti, si = header.index("timestamp"), header.index("symbol")
            for row in reader:
                rows += 1
                wrong_symbol += row[si] != traded
                outside += not first <= float(row[ti]) < last
    except (OSError, EOFError, ValueError, IndexError, csv.Error) as exc:
        return [f"cannot read it as a gzip CSV: {type(exc).__name__}: {exc}"]
    problems: list[str] = []
    if rows == 0:
        problems.append("no trades in the file")
    if wrong_symbol:
        problems.append(f"{wrong_symbol} of {rows} rows are not {traded}")
    if outside:
        problems.append(f"{outside} of {rows} rows fall outside {entry.period} (UTC)")
    return problems
