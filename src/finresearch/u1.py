"""Building U1's point-in-time tables from the exchange archives (Phase 2.5, decision 0010).

``config/u1.toml`` names the tables; under each table, the exchanges that feed
it and the archive datasets they come from. A *job* is one table from one
exchange; ``PRODUCTS`` holds the code that turns that exchange's archive files
into that table's rows. For one job, one build makes one month:

1. replay the archive manifests (``data/manifest.py``) to learn which files
   exist; for each contract in the exchange's universe take its monthly file,
   or its daily files when the month has no monthly file;
2. download every file, checking size, ETag and the official SHA-256 where
   the archive publishes one (``archive.download``), and parse it into rows;
3. add the rows symbol by symbol to one partition
   (``canonical/<table>/v<format>/<month>/<exchange>``), whose card lists every
   input file with its SHA-256 and what the manifest said about it, and notes
   any rows that are well formed but impossible;
4. push the partition. A partition that is already in the store is never
   rebuilt (the store is append-only): ``plan`` leaves it out, and reports it
   if the archive has changed under it since: other files, or the same files
   published again under the same address.

Only months whose latest manifest listing came ``settle_days`` after the month
ended are built, so the listing has seen the monthly files. A contract-month
with daily files but no monthly file (the archive's first days, a contract
delisted early in a month) is built from the daily files.
"""

import json
import random
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from finresearch import archive
from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc, month_start
from finresearch.data import bars, funding, mark
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
from finresearch.sources import (
    binance_archive,
    binance_funding,
    binance_klines,
    binance_mark,
    binance_trades,
    bybit_archive,
    bybit_trades,
)


class U1Error(RuntimeError):
    """The configuration, the manifests or the archive do not allow the requested build."""


def u1_config() -> dict[str, Any]:
    return load_toml("u1")


type Parser = Callable[[bytes, ManifestEntry, datetime], PitTable]


@dataclass(frozen=True)
class Product:
    """How one exchange's archive files become rows of one table."""

    table: str
    keys: tuple[str, ...]
    lister: archive.Lister
    # Whether the download checks what the file holds (the official checksum, the zip).
    # Off where the parser reads every row anyway and checks them as it goes.
    check_content: bool
    parse: Parser  # one file's bytes, its manifest entry, its publication time -> rows
    concat: Callable[[Sequence[PitTable]], PitTable]  # one symbol's files, in period order
    problems: Callable[[PitTable], list[str]]  # rows kept but impossible, for the card's notes


def _binance_klines(content: bytes, e: ManifestEntry, published: datetime) -> PitTable:
    return binance_klines.parse_klines(binance_klines.csv_from_zip(content, e), e, published)


def _binance_funding(content: bytes, e: ManifestEntry, published: datetime) -> PitTable:
    return binance_funding.parse_funding(binance_klines.csv_from_zip(content, e), e, published)


def _binance_mark(content: bytes, e: ManifestEntry, published: datetime) -> PitTable:
    return binance_mark.parse_mark(binance_klines.csv_from_zip(content, e), e, published)


def _no_problems(_: PitTable) -> list[str]:
    return []


PRODUCTS: dict[tuple[str, str], Product] = {
    (bars.NAME, "binance"): Product(
        bars.NAME, bars.KEYS, binance_archive, True, _binance_klines, bars.concat, bars.problems
    ),
    (bars.NAME, "bybit"): Product(
        bars.NAME,
        bars.KEYS,
        bybit_archive,
        False,
        bybit_trades.parse_trades,
        bars.concat,
        bars.problems,
    ),
    (funding.NAME, "binance"): Product(
        funding.NAME,
        funding.KEYS,
        binance_archive,
        True,
        _binance_funding,
        funding.concat,
        _no_problems,
    ),
    (mark.NAME, "binance"): Product(
        mark.NAME, mark.KEYS, binance_archive, True, _binance_mark, mark.concat, mark.problems
    ),
}


@dataclass(frozen=True)
class Job:
    """One table from one exchange, as configured."""

    table: str
    exchange: str
    format: int
    universe: str  # regular expression over the archive's symbol folders
    monthly: str | None  # archive dataset of monthly files, if the archive has them
    daily: str | None  # archive dataset of daily files, if the archive has them
    settle_days: int
    workers: int
    batch: int

    @property
    def product(self) -> Product:
        return PRODUCTS[(self.table, self.exchange)]

    @property
    def label(self) -> str:
        return f"{self.table} {self.exchange}"


def job(cfg: Mapping[str, Any], table: str, exchange: str) -> Job:
    """The configured job for ``table`` from ``exchange``."""
    tables = cfg.get("tables", {})
    if table not in tables or exchange not in tables[table]:
        raise U1Error(f"config/u1.toml has no {table} from {exchange}")
    if (table, exchange) not in PRODUCTS:
        raise U1Error(f"no code builds {table} from {exchange}")
    t, ex = tables[table], tables[table][exchange]
    monthly, daily = ex.get("monthly"), ex.get("daily")
    if monthly is None and daily is None:
        raise U1Error(f"{table} from {exchange}: name a monthly or a daily dataset")
    return Job(
        table=table,
        exchange=exchange,
        format=int(t["format"]),
        universe=str(cfg["universe"][exchange]),
        monthly=None if monthly is None else str(monthly),
        daily=None if daily is None else str(daily),
        settle_days=int(ex["settle_days"]),
        workers=int(ex["workers"]),
        batch=int(ex["batch"]),
    )


def jobs(
    cfg: Mapping[str, Any], tables: Sequence[str] | None = None, exchange: str | None = None
) -> list[Job]:
    """The configured jobs, in configuration order, limited to ``tables`` and ``exchange``."""
    out: list[Job] = []
    for table, spec in cfg.get("tables", {}).items():
        if tables is not None and table not in tables:
            continue
        for ex, value in spec.items():
            if isinstance(value, Mapping) and (exchange is None or ex == exchange):
                out.append(job(cfg, table, ex))
    return out


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
    def entries(self) -> list[ManifestEntry]:
        return sorted((e for es in self.files.values() for e in es), key=lambda e: e.url)

    @property
    def urls(self) -> list[str]:
        return [e.url for e in self.entries]


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


def listed(e: ManifestEntry) -> str:
    """What the manifest says about a file, as one string: a file published again differs."""
    when = None if e.last_modified is None else ensure_utc(e.last_modified).isoformat()
    return json.dumps([e.size, e.etag, when, e.checksum_url])


@dataclass
class Plan:
    months: list[date]  # to build
    stale: list[str] = field(default_factory=lambda: list[str]())  # built, but inputs changed since


def _manifests(
    j: Job, remote: Remote, limits: Limits, work: Path
) -> tuple[list[ManifestEntry], list[ManifestEntry], datetime]:
    """Monthly and daily entries (empty where the archive has none) and the older listing time."""
    loaded = {
        kind: load_manifest(dataset, remote, limits, work)
        for kind, dataset in (("monthly", j.monthly), ("daily", j.daily))
        if dataset is not None
    }
    monthly = loaded["monthly"].entries if "monthly" in loaded else []
    daily = loaded["daily"].entries if "daily" in loaded else []
    return monthly, daily, min(m.listed_at for m in loaded.values())


def _staleness(card: PartitionCard, chosen: MonthInputs) -> str | None:
    """Why a built partition no longer matches the archive, if it does not."""
    month = card.month
    if sorted(card.inputs) != chosen.urls:
        return f"{month}: the archive now has other files than the partition"
    if card.listed:  # cards written before 2.5.6 do not record it
        again = sum(1 for e in chosen.entries if card.listed.get(e.url) != listed(e))
        if again:
            return f"{month}: {again} file(s) published again since the partition was built"
    return None


def plan(j: Job, remote: Remote, limits: Limits, work: Path) -> Plan:
    """Months whose partition of ``j`` is not in the store yet; built ones gone stale."""
    monthly, daily, listed_at = _manifests(j, remote, limits, work)
    result = Plan([])
    for m in buildable_months([*monthly, *daily], j.universe, listed_at, j.settle_days):
        folder = month_dir(j.table, j.format, m)
        card_path = f"{folder}/{j.exchange}.json"
        if card_path not in remote.list(folder):
            result.months.append(m)
            continue
        card = PartitionCard.from_json(remote.read(card_path).decode("utf-8"))
        reason = _staleness(card, select(monthly, daily, j.universe, m))
        if reason is not None:
            result.stale.append(reason)
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


def file_rows(product: Product, e: ManifestEntry) -> tuple[PitTable, str]:
    """Download, check and parse one archive file; return its rows and its SHA-256.

    ``ingested_at`` is the file's publication time: from the manifest where the
    listing gives it (Binance), otherwise from the download's Last-Modified.
    """
    content, check = archive.download(e, product.lister, content=product.check_content)
    if not check.ok:
        raise U1Error(f"{e.url}: {'; '.join(check.problems)}")
    published = e.last_modified or check.last_modified
    if published is None:
        raise U1Error(f"{e.url}: neither the manifest nor the server gives a publication time")
    return product.parse(content, e, published), check.sha256


def _symbol_rows(
    product: Product, entries: Sequence[ManifestEntry]
) -> tuple[PitTable, dict[str, str], list[str]]:
    """One symbol's rows for the month, the files they came from, and the files without rows."""
    tables: list[PitTable] = []
    inputs: dict[str, str] = {}
    empty: list[str] = []
    for e in entries:
        table, sha = file_rows(product, e)
        tables.append(table)
        inputs[e.url] = sha
        if table.num_rows == 0:
            empty.append(e.url)
    return (tables[0] if len(tables) == 1 else product.concat(tables)), inputs, empty


def build(
    j: Job, month: date, remote: Remote, limits: Limits, work: Path, row_group_rows: int
) -> BuildResult:
    """Build and push the partition of ``month`` for job ``j``."""
    product = j.product
    folder = month_dir(j.table, j.format, month)
    if f"{folder}/{j.exchange}.json" in remote.list(folder):
        raise U1Error(f"{folder}/{j.exchange} is already in the store; it is never rebuilt")
    monthly, daily, _ = _manifests(j, remote, limits, work / "manifests")
    chosen = select(monthly, daily, j.universe, month)
    if not chosen.files:
        raise U1Error(f"no {month:%Y-%m} files for the universe in the manifests")
    out = work / "out"
    writer = PartitionWriter(
        out, j.table, product.keys, j.format, month, j.exchange, row_group_rows
    )
    inputs: dict[str, str] = {}
    notes: list[str] = []

    empty: list[str] = []

    def one(symbol: str) -> tuple[PitTable, dict[str, str], list[str]]:
        return _symbol_rows(product, chosen.files[symbol])

    with ThreadPoolExecutor(max_workers=j.workers) as pool:
        for batch in _batches(list(chosen.files), j.batch):
            for table, used, without_rows in pool.map(one, batch):
                writer.add(table)
                inputs.update(used)
                notes.extend(product.problems(table))
                empty.extend(without_rows)
    if empty:
        notes.append(f"input files without rows: {len(empty)}, first {min(empty)}")
    card = writer.close(inputs, notes, {e.url: listed(e) for e in chosen.entries})
    from_daily = (
        [s for s, es in chosen.files.items() if es[0].period != f"{month:%Y-%m}"]
        if j.monthly is not None
        else []
    )
    pushed = push(out, remote, folder, limits, f"{j.table} {month:%Y-%m} {j.exchange}")
    return BuildResult(month, card, from_daily, pushed)


@dataclass
class CrossCheck:
    """A cross-check of klines against trades. The numbers are a quality report
    derived from the data (decision 0008): they go to the private dataset only."""

    report: str  # where the report was stored
    days: int
    minutes: int
    passed: bool
    pushed: PushReport


def crosscheck(
    cfg: Mapping[str, Any],
    remote: Remote,
    limits: Limits,
    work: Path,
    now: datetime,
    seed: int,
) -> CrossCheck:
    """Rebuild a random sample of Binance contract-days from trades and compare with the klines.

    Decision 0010 takes Binance's 1-minute data from its klines; this is the
    check that keeps that choice honest. A minute disagrees when either table
    has it and the other does not, or any field differs exactly. The share of
    disagreeing minutes must stay within ``max_differing_share``.
    """
    cc = cfg["crosscheck"]
    universe = re.compile(str(cfg["universe"]["binance"]))
    klines_product = PRODUCTS[(bars.NAME, "binance")]
    trades = load_manifest(str(cc["trades"]), remote, limits, work / "manifests").entries
    klines = load_manifest(str(cc["klines"]), remote, limits, work / "manifests").entries
    kline_by = {(e.symbol, e.period): e for e in klines if universe.fullmatch(e.symbol)}
    pairs = sorted(
        ((kline_by[(e.symbol, e.period)], e) for e in trades if (e.symbol, e.period) in kline_by),
        key=lambda p: p[1].url,
    )
    rng = random.Random(seed)  # noqa: S311 - a reproducible sample, not a secret
    sample = rng.sample(pairs, min(int(cc["sample"]), len(pairs)))

    def one(pair: tuple[ManifestEntry, ManifestEntry]) -> dict[str, Any]:
        kline, trade = pair
        reference, _ = file_rows(klines_product, kline)
        content, check = archive.download(trade, binance_archive)
        if not check.ok:
            raise U1Error(f"{trade.url}: {'; '.join(check.problems)}")
        published = trade.last_modified or check.last_modified or ensure_utc(now)
        rebuilt = binance_trades.parse_trades(content, trade, published)
        return {"symbol": trade.symbol, "day": trade.period, **bars.compare(reference, rebuilt)}

    with ThreadPoolExecutor(max_workers=int(cc["workers"])) as pool:
        rows = list(pool.map(one, sample))
    totals = (
        {k: sum(int(r[k]) for r in rows) for k in rows[0] if k not in ("symbol", "day")}
        if rows
        else {}
    )
    disagreeing = totals.get("differing", 0) + totals.get("missing", 0) + totals.get("extra", 0)
    minutes = totals.get("minutes", 0)
    share = disagreeing / minutes if minutes else 0.0
    passed = share <= float(cc["max_differing_share"])
    stamp = f"{ensure_utc(now):%Y%m%dT%H%M%SZ}"
    path = f"quality/u1-crosscheck/{stamp}.json"
    out = work / "out"
    (out / path).parent.mkdir(parents=True, exist_ok=True)
    report = {
        "checked_at": stamp,
        "seed": seed,
        "threshold": float(cc["max_differing_share"]),
        "share_differing": share,
        "passed": passed,
        "totals": totals,
        "days": rows,
    }
    (out / path).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    pushed = push(out, remote, "quality/u1-crosscheck", limits, f"u1 cross-check {stamp}")
    return CrossCheck(path, len(rows), minutes, passed, pushed)
