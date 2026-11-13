"""Building U1's 1-minute bars from the exchange archives (Phase 2.5, decision 0010).

``config/u1.toml`` says which contracts are in the universe and which archive
datasets feed them. For Binance, one job builds one month:

1. replay the archive manifests (``data/manifest.py``) to learn which files
   exist; for each contract in the universe take its monthly kline file, or
   its daily files when the month has no monthly file;
2. download every file, checking size, ETag and the official SHA-256
   (``archive.download``), and parse it into bars;
3. add the bars symbol by symbol to one partition
   (``canonical/u1.bars_1m/v<format>/<month>/binance``), whose card lists every
   input file with its SHA-256 and notes any impossible bars;
4. push the partition. A partition that is already in the store is never
   rebuilt (the store is append-only): ``plan`` leaves it out, and reports it
   if the archive has changed under it since.

Only months whose latest manifest listing came ``settle_days`` after the month
ended are built, so the listing has seen the monthly files. A contract-month
with daily files but no monthly file (the archive's first days, a contract
delisted early in a month) is built from the daily files.
"""

import re
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from finresearch import archive
from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc, month_start
from finresearch.data import bars
from finresearch.data.manifest import (
    PREFIX,
    ManifestEntry,
    increment_time,
    read_increment,
    replay,
)
from finresearch.data.partitioned import PartitionCard, PartitionWriter, month_dir
from finresearch.data.store import Limits, PushReport, Remote, local_files, pull, push
from finresearch.data.table import PitTable
from finresearch.sources import binance_archive, binance_klines, bybit_archive, bybit_trades


class U1Error(RuntimeError):
    """The configuration, the manifests or the archive do not allow the requested build."""


def u1_config() -> dict[str, Any]:
    return load_toml("u1")


@dataclass(frozen=True)
class Manifest:
    entries: list[ManifestEntry]
    listed_at: datetime  # when the latest listing behind it started


def load_manifest(dataset: str, remote: Remote, limits: Limits, work: Path) -> Manifest:
    """What the archive held at the dataset's latest listing."""
    prefix = f"{PREFIX}/{dataset}"
    pull(remote, work, prefix, limits)
    paths = local_files(work, prefix)
    listed = [t for p in paths if (t := increment_time(p)) is not None]
    if not listed:
        raise U1Error(f"no manifest for {dataset}: run the archive workflow for it first")
    entries = list(replay(read_increment(work / p) for p in paths).values())
    return Manifest(entries, max(listed))


def _month(period: str) -> date:
    return date(int(period[:4]), int(period[5:7]), 1)


@dataclass(frozen=True)
class MonthInputs:
    """The files one month is built from, by symbol, symbols in order."""

    month: date
    files: dict[str, list[ManifestEntry]]

    @property
    def urls(self) -> list[str]:
        return sorted(e.url for es in self.files.values() for e in es)


def select(
    monthly: Sequence[ManifestEntry], daily: Sequence[ManifestEntry], universe: str, month: date
) -> MonthInputs:
    """For each contract in ``universe`` (a regular expression), its files for ``month``.

    The monthly file when there is one, otherwise the daily files of that month.
    """
    pattern = re.compile(universe)
    whole = {
        e.symbol: e for e in monthly if pattern.fullmatch(e.symbol) and _month(e.period) == month
    }
    days: dict[str, list[ManifestEntry]] = {}
    for e in daily:
        if pattern.fullmatch(e.symbol) and _month(e.period) == month and e.symbol not in whole:
            days.setdefault(e.symbol, []).append(e)
    files = {
        s: [whole[s]] if s in whole else sorted(days[s], key=lambda e: e.period)
        for s in sorted(whole.keys() | days.keys())
    }
    return MonthInputs(month, files)


def buildable_months(
    entries: Sequence[ManifestEntry], universe: str, listed_at: datetime, settle_days: int
) -> list[date]:
    """Months with files for the universe whose listing came ``settle_days`` after they ended.

    A listing taken that long after a month's end has seen its monthly files,
    which appear in the first days of the next month.
    """
    pattern = re.compile(universe)
    listed = ensure_utc(listed_at).date()
    months = {_month(e.period) for e in entries if pattern.fullmatch(e.symbol)}
    return sorted(m for m in months if listed >= month_start(m, 1) + timedelta(days=settle_days))


@dataclass
class Plan:
    months: list[date]  # to build
    stale: list[str] = field(default_factory=lambda: list[str]())  # built, but inputs changed since


EXCHANGES = ("binance", "bybit")


def _manifests(
    ex: Mapping[str, Any], remote: Remote, limits: Limits, work: Path
) -> tuple[list[ManifestEntry], list[ManifestEntry], datetime]:
    """Monthly entries (empty where the archive has none), daily entries, and the older listing."""
    daily = load_manifest(str(ex["daily"]), remote, limits, work)
    if "monthly" not in ex:
        return [], daily.entries, daily.listed_at
    monthly = load_manifest(str(ex["monthly"]), remote, limits, work)
    return monthly.entries, daily.entries, min(monthly.listed_at, daily.listed_at)


def plan(exchange: str, cfg: Mapping[str, Any], remote: Remote, limits: Limits, work: Path) -> Plan:
    """Months whose ``exchange`` partition is not in the store yet; built ones gone stale."""
    ex = cfg[exchange]
    fmt = int(cfg["bars"]["format"])
    monthly, daily, listed = _manifests(ex, remote, limits, work)
    universe = str(ex["universe"])
    result = Plan([])
    for m in buildable_months([*monthly, *daily], universe, listed, int(ex["settle_days"])):
        folder = month_dir(bars.NAME, fmt, m)
        card_path = f"{folder}/{ex['part']}.json"
        if card_path not in remote.list(folder):
            result.months.append(m)
            continue
        card = PartitionCard.from_json(remote.read(card_path).decode("utf-8"))
        if sorted(card.inputs) != select(monthly, daily, universe, m).urls:
            result.stale.append(f"{m:%Y-%m}: the archive now has other files than the partition")
    return result


@dataclass
class BuildResult:
    month: date
    card: PartitionCard
    from_daily: list[str]  # symbols built from daily files where the archive has monthly ones
    pushed: PushReport


def _batches[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _file_bars(exchange: str, e: ManifestEntry) -> tuple[PitTable, str]:
    """Download, check and parse one archive file; return its bars and its SHA-256.

    ``ingested_at`` is the file's publication time: from the manifest where the
    listing gives it (Binance), otherwise from the download's Last-Modified.
    """
    if exchange == "binance":
        content, check = archive.download(e, binance_archive)  # checks the official SHA-256
    else:
        # The trade parser reads every row anyway and checks symbol and day as it goes.
        content, check = archive.download(e, bybit_archive, content=False)
    if not check.ok:
        raise U1Error(f"{e.url}: {'; '.join(check.problems)}")
    published = e.last_modified or check.last_modified
    if published is None:
        raise U1Error(f"{e.url}: neither the manifest nor the server gives a publication time")
    if exchange == "binance":
        table = binance_klines.parse_klines(binance_klines.csv_from_zip(content, e), e, published)
    else:
        table = bybit_trades.parse_trades(content, e, published)
    return table, check.sha256


def _symbol_bars(
    exchange: str, entries: Sequence[ManifestEntry]
) -> tuple[PitTable, dict[str, str]]:
    """One symbol's bars for the month, and the files they came from."""
    tables: list[PitTable] = []
    inputs: dict[str, str] = {}
    for e in entries:
        table, sha = _file_bars(exchange, e)
        tables.append(table)
        inputs[e.url] = sha
    return (tables[0] if len(tables) == 1 else bars.concat(tables)), inputs


def build(
    exchange: str,
    month: date,
    cfg: Mapping[str, Any],
    remote: Remote,
    limits: Limits,
    work: Path,
    row_group_rows: int,
) -> BuildResult:
    """Build and push the partition of ``month`` for ``exchange``."""
    ex = cfg[exchange]
    fmt = int(cfg["bars"]["format"])
    folder = month_dir(bars.NAME, fmt, month)
    if f"{folder}/{ex['part']}.json" in remote.list(folder):
        raise U1Error(f"{folder}/{ex['part']} is already in the store; it is never rebuilt")
    monthly, daily, _ = _manifests(ex, remote, limits, work / "manifests")
    chosen = select(monthly, daily, str(ex["universe"]), month)
    if not chosen.files:
        raise U1Error(f"no {month:%Y-%m} files for the universe in the manifests")
    out = work / "out"
    writer = PartitionWriter(out, bars.NAME, bars.KEYS, fmt, month, str(ex["part"]), row_group_rows)
    inputs: dict[str, str] = {}
    notes: list[str] = []

    def one(symbol: str) -> tuple[PitTable, dict[str, str]]:
        return _symbol_bars(exchange, chosen.files[symbol])

    with ThreadPoolExecutor(max_workers=int(ex["workers"])) as pool:
        for batch in _batches(list(chosen.files), int(ex["batch"])):
            for table, used in pool.map(one, batch):
                writer.add(table)
                inputs.update(used)
                notes.extend(bars.problems(table))
    card = writer.close(inputs, notes)
    from_daily = (
        [s for s, es in chosen.files.items() if es[0].period != f"{month:%Y-%m}"]
        if "monthly" in ex
        else []
    )
    pushed = push(out, remote, folder, limits, f"u1.bars_1m {month:%Y-%m} {ex['part']}")
    return BuildResult(month, card, from_daily, pushed)
