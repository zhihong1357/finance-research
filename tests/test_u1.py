"""Building U1 bars from a fake Binance archive: hand-written klines, checksums and
manifests, and a local stand-in for the remote store."""

import gzip
import hashlib
import io
import json
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from finresearch import u1
from finresearch.core.config import load_toml
from finresearch.core.time import utc
from finresearch.data.manifest import Change, ManifestEntry, Row, increment_path, write_increment
from finresearch.data.partitioned import PartitionCard, PartitionedTable
from finresearch.data.store import DirectoryRemote, Limits
from tests.sources.conftest import FakeServer, install

LIMITS = Limits(
    max_files_per_commit=50, max_entries_per_folder=9000, retries=0, retry_wait_seconds=0
)
CFG: dict[str, Any] = {
    "bars": {"format": 1},
    "binance": {
        "universe": "^[^_]+USDT$",
        "part": "binance",
        "monthly": "m",
        "daily": "d",
        "settle_days": 7,
        "workers": 2,
        "batch": 1,
    },
}
FILES = "https://data.binance.vision/data/futures/um/"
OCTOBER = date(2026, 10, 1)
# 2026-10-01 00:00 UTC is 1,790,812,800 seconds after the epoch (20727 days).
OCT_MS = 1_790_812_800_000
DAY_MS = 86_400_000


def _kline(ms: int, close: str = "1.5") -> str:
    return f"{ms},1.0,2.0,0.5,{close},3.000,{ms + 59_999},4.5,6,1.000,1.5,0"


def _zip(name: str, rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name.removesuffix(".zip") + ".csv", "\n".join(rows) + "\n")
    return buf.getvalue()


class FakeArchive:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.entries: dict[str, list[ManifestEntry]] = {"m": [], "d": []}

    def put(self, dataset: str, symbol: str, period: str, rows: list[str]) -> None:
        kind = "monthly" if dataset == "m" else "daily"
        name = f"{symbol}-1m-{period}.zip"
        url = f"{FILES}{kind}/klines/{symbol}/1m/{name}"
        content = _zip(name, rows)
        self.files[url] = content
        self.files[url + ".CHECKSUM"] = f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode()
        md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        self.entries[dataset].append(
            ManifestEntry(
                dataset, symbol, period, url, len(content), md5, utc(2026, 11, 2), url + ".CHECKSUM"
            )
        )

    def put_trades(self, symbol: str, day: str, rows: list[str]) -> None:
        name = f"{symbol}-trades-{day}.zip"
        url = f"{FILES}daily/trades/{symbol}/{name}"
        content = _zip(name, ["id,price,qty,quote_qty,time,is_buyer_maker", *rows])
        self.files[url] = content
        self.files[url + ".CHECKSUM"] = f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode()
        md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        self.entries.setdefault("t", []).append(
            ManifestEntry(
                "t", symbol, day, url, len(content), md5, utc(2026, 11, 2), url + ".CHECKSUM"
            )
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        content = self.files[str(request.url)]
        etag = hashlib.md5(content, usedforsecurity=False).hexdigest()
        return httpx2.Response(200, content=content, headers={"ETag": f'"{etag}"'})

    def publish(self, remote: DirectoryRemote, listed: datetime) -> None:
        """Write each dataset's manifest into the store as one increment."""
        for dataset, entries in self.entries.items():
            rows = [Row(Change.ADDED, e) for e in entries]
            write_increment(rows, remote.root / increment_path(dataset, listed))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeArchive:
    server: FakeServer = install(monkeypatch)
    archive = FakeArchive()
    server.handler = archive
    return archive


def _october(fake: FakeArchive) -> None:
    fake.put("m", "AAAUSDT", "2026-10", [_kline(OCT_MS + 60_000), _kline(OCT_MS)])
    # BBBUSDT has no monthly file for October: two daily files instead.
    fake.put("d", "BBBUSDT", "2026-10-01", [_kline(OCT_MS + 120_000)])
    fake.put("d", "BBBUSDT", "2026-10-02", [_kline(OCT_MS + DAY_MS)])
    fake.put("d", "AAAUSDT", "2026-10-01", [_kline(OCT_MS)])  # ignored: the monthly file wins
    fake.put("m", "CCCUSDT_261226", "2026-10", [_kline(OCT_MS)])  # dated: not in the universe
    fake.put("m", "DDDUSDTSETTLED", "2026-10", [_kline(OCT_MS)])  # not a contract of its own


def test_the_monthly_file_wins_and_daily_files_fill_in(fake: FakeArchive) -> None:
    _october(fake)
    chosen = u1.select(fake.entries["m"], fake.entries["d"], CFG["binance"]["universe"], OCTOBER)
    assert list(chosen.files) == ["AAAUSDT", "BBBUSDT"]
    assert [e.period for e in chosen.files["AAAUSDT"]] == ["2026-10"]
    assert [e.period for e in chosen.files["BBBUSDT"]] == ["2026-10-01", "2026-10-02"]


def test_a_month_is_built_once_it_has_settled(fake: FakeArchive) -> None:
    _october(fake)
    fake.put("d", "AAAUSDT", "2026-11-01", [_kline(OCT_MS + 31 * DAY_MS)])
    entries = [*fake.entries["m"], *fake.entries["d"]]
    universe = CFG["binance"]["universe"]
    assert u1.buildable_months(entries, universe, utc(2026, 11, 7, 23), 7) == []  # listed too soon
    assert u1.buildable_months(entries, universe, utc(2026, 11, 8), 7) == [OCTOBER]


def test_a_listing_too_soon_after_the_month_plans_nothing(
    fake: FakeArchive, tmp_path: Path
) -> None:
    _october(fake)
    remote = DirectoryRemote(tmp_path / "remote")
    fake.publish(remote, utc(2026, 11, 3))
    assert u1.plan("binance", CFG, remote, LIMITS, tmp_path / "p").months == []


def test_a_month_becomes_one_partition_with_its_inputs(fake: FakeArchive, tmp_path: Path) -> None:
    _october(fake)
    remote = DirectoryRemote(tmp_path / "remote")
    fake.publish(remote, utc(2026, 11, 9))
    plan = u1.plan("binance", CFG, remote, LIMITS, tmp_path / "p1")
    assert (plan.months, plan.stale) == ([OCTOBER], [])

    result = u1.build("binance", OCTOBER, CFG, remote, LIMITS, tmp_path / "b1", row_group_rows=2)
    assert result.from_daily == ["BBBUSDT"]
    card = result.card
    assert (card.name, card.rows, card.notes) == ("u1.bars_1m/v1/2026-10/binance", 4, [])
    assert sorted(card.inputs) == sorted(
        e.url for e in fake.entries["m"][:1] + fake.entries["d"][:2]
    )
    stored = PartitionCard.from_json(
        (remote.root / "canonical/u1.bars_1m/v1/2026-10/binance.json").read_text()
    )
    assert stored == card

    table = PartitionedTable(
        "u1.bars_1m", ("exchange", "symbol"), 1, tmp_path / "read", remote, LIMITS
    )
    w = table.window(None, utc(2026, 11, 1), where={"symbol": {"BBBUSDT"}}, columns=["close"])
    assert w.table.column("event_time").to_pylist() == [utc(2026, 10, 1, 0, 2), utc(2026, 10, 2)]
    assert w.table.column("ingested_at").to_pylist() == [utc(2026, 11, 2)] * 2

    plan = u1.plan("binance", CFG, remote, LIMITS, tmp_path / "p2")
    assert (plan.months, plan.stale) == ([], [])
    with pytest.raises(u1.U1Error, match="never rebuilt"):
        u1.build("binance", OCTOBER, CFG, remote, LIMITS, tmp_path / "b2", row_group_rows=2)

    fake.put("d", "EEEUSDT", "2026-10-31", [_kline(OCT_MS + 30 * DAY_MS)])  # appeared later
    fake.publish(remote, utc(2026, 11, 16))
    plan = u1.plan("binance", CFG, remote, LIMITS, tmp_path / "p3")
    assert plan.stale == ["2026-10: the archive now has other files than the partition"]


def test_a_file_that_fails_its_checksum_stops_the_build(fake: FakeArchive, tmp_path: Path) -> None:
    _october(fake)
    url = fake.entries["m"][0].url
    fake.files[url + ".CHECKSUM"] = f"{'0' * 64}  AAAUSDT-1m-2026-10.zip\n".encode()
    remote = DirectoryRemote(tmp_path / "remote")
    fake.publish(remote, utc(2026, 11, 9))
    with pytest.raises(u1.U1Error, match="SHA-256"):
        u1.build("binance", OCTOBER, CFG, remote, LIMITS, tmp_path / "b", row_group_rows=2)
    assert remote.list("canonical") == {}


def test_without_manifests_nothing_can_be_planned(tmp_path: Path) -> None:
    with pytest.raises(u1.U1Error, match="run the archive workflow"):
        u1.plan("binance", CFG, DirectoryRemote(tmp_path), LIMITS, tmp_path / "w")


def test_the_configured_universe_and_datasets_exist() -> None:
    cfg = u1.u1_config()
    archives = load_toml("archives")["datasets"]
    b = cfg["binance"]
    assert archives[b["monthly"]]["enabled"]
    assert archives[b["daily"]]["enabled"]
    pattern = re.compile(b["universe"])
    assert pattern.fullmatch("BTCUSDT")
    assert pattern.fullmatch("币安人生USDT")
    for other in ("BTCUSDT_250328", "BTCUSDC", "ETHBTC", "ICPUSDT_SETTLED", "TLMUSDTSETTLED"):
        assert not pattern.fullmatch(other), other


BYBIT_CFG: dict[str, Any] = {
    "bars": {"format": 1},
    "bybit": {
        "universe": "^[^_-]+USDT$",
        "part": "bybit",
        "daily": "b",
        "settle_days": 7,
        "workers": 2,
        "batch": 1,
    },
}
TRADING = "https://public.bybit.com/trading/"
OCT_S = OCT_MS // 1000


class FakeBybit:
    """Bybit's archive: one gzip CSV of trades per contract-day, described as HEAD would."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.entries: list[ManifestEntry] = []

    def put(self, folder: str, traded: str, day: str, seconds: list[int]) -> None:
        url = f"{TRADING}{folder}/{traded}{day}.csv.gz"
        rows = [f"{s},{traded},Buy,1.5,2.25,PlusTick,id,1,1,1" for s in seconds]
        header = "timestamp,symbol,side,size,price,tickDirection,trdMatchID,a,b,c"
        content = gzip.compress(("\n".join([header, *rows]) + "\n").encode())
        self.files[url] = content
        md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        self.entries.append(
            ManifestEntry("b", folder, day, url, len(content), md5, utc(2026, 11, 2))
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        content = self.files[str(request.url)]
        etag = hashlib.md5(content, usedforsecurity=False).hexdigest()
        headers = {"ETag": f'"{etag}"', "Last-Modified": "Wed, 04 Nov 2026 01:02:03 GMT"}
        return httpx2.Response(200, content=content, headers=headers)


def test_without_a_description_the_download_gives_the_publication_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeBybit()
    install(monkeypatch).handler = fake
    fake.put("AAAUSDT", "AAAUSDT", "2026-10-01", [OCT_S + 1])
    named_only = [ManifestEntry("b", e.symbol, e.period, e.url) for e in fake.entries]
    remote = DirectoryRemote(tmp_path / "remote")
    write_increment(
        [Row(Change.ADDED, e) for e in named_only],
        remote.root / increment_path("b", utc(2026, 11, 9)),
    )
    u1.build("bybit", OCTOBER, BYBIT_CFG, remote, LIMITS, tmp_path / "w", row_group_rows=2)
    table = PartitionedTable(
        "u1.bars_1m", ("exchange", "symbol"), 1, tmp_path / "read", remote, LIMITS
    )
    w = table.window(None, utc(2026, 11, 5))
    assert w.table.column("ingested_at").to_pylist() == [utc(2026, 11, 4, 1, 2, 3)]


def test_bybit_trades_become_the_bybit_partition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeBybit()
    install(monkeypatch).handler = fake
    fake.put("AAAUSDT", "AAAUSDT", "2026-10-01", [OCT_S + 1, OCT_S + 61])
    fake.put("AAAUSDT", "AAAUSDT", "2026-10-02", [OCT_S + 86_400 + 5])
    fake.put("BBBOLDUSDT", "BBBUSDT", "2026-10-01", [OCT_S + 7])  # the older contract of BBBUSDT
    fake.put("AAAUSDT-26DEC26", "AAAUSDT-26DEC26", "2026-10-01", [OCT_S])  # dated: not in U1
    fake.put("WC_X_USDT-26DEC26", "WC_X_USDT-26DEC26", "2026-10-01", [OCT_S])  # event contract
    fake.put("AAAUSD", "AAAUSD", "2026-10-01", [OCT_S])  # inverse
    remote = DirectoryRemote(tmp_path / "remote")
    write_increment(
        [Row(Change.ADDED, e) for e in fake.entries],
        remote.root / increment_path("b", utc(2026, 11, 9)),
    )
    assert u1.plan("bybit", BYBIT_CFG, remote, LIMITS, tmp_path / "p").months == [OCTOBER]
    result = u1.build("bybit", OCTOBER, BYBIT_CFG, remote, LIMITS, tmp_path / "w", row_group_rows=2)
    assert (result.card.name, result.card.rows, result.from_daily) == (
        "u1.bars_1m/v1/2026-10/bybit",
        4,
        [],
    )
    assert len(result.card.inputs) == 3
    table = PartitionedTable(
        "u1.bars_1m", ("exchange", "symbol"), 1, tmp_path / "read", remote, LIMITS
    )
    w = table.window(None, utc(2026, 11, 1), where={"exchange": {"bybit"}}, columns=["trades"])
    assert set(w.table.column("symbol").to_pylist()) == {"AAAUSDT", "BBBOLDUSDT"}
    assert w.table.column("trades").to_pylist() == [1, 1, 1, 1]


def test_a_bybit_file_that_changed_since_it_was_described_stops_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeBybit()
    install(monkeypatch).handler = fake
    fake.put("AAAUSDT", "AAAUSDT", "2026-10-01", [OCT_S + 1])
    fake.files[fake.entries[0].url] += b"\0"  # replaced after the listing described it
    remote = DirectoryRemote(tmp_path / "remote")
    write_increment(
        [Row(Change.ADDED, e) for e in fake.entries],
        remote.root / increment_path("b", utc(2026, 11, 9)),
    )
    with pytest.raises(u1.U1Error, match="bytes, the manifest lists"):
        u1.build("bybit", OCTOBER, BYBIT_CFG, remote, LIMITS, tmp_path / "w", row_group_rows=2)
    assert remote.list("canonical") == {}


CROSSCHECK_CFG: dict[str, Any] = {
    **CFG,
    "crosscheck": {"trades": "t", "klines": "d", "sample": 5, "max_differing_share": 0.0},
}


def _kline_of(ms: int, trades: list[tuple[str, str, bool]]) -> str:
    """The kline a list of (price, qty, taker buys) trades in one minute should give."""
    prices = [Decimal(p) for p, _, _ in trades]
    qty = sum(Decimal(q) for _, q, _ in trades)
    quote = sum(Decimal(p) * Decimal(q) for p, q, _ in trades)
    buy = sum(Decimal(q) for _, q, b in trades if b)
    buy_quote = sum(Decimal(p) * Decimal(q) for p, q, b in trades if b)
    return (
        f"{ms},{prices[0]},{max(prices)},{min(prices)},{prices[-1]},{qty},{ms + 59_999},"
        f"{quote},{len(trades)},{buy},{buy_quote},0"
    )


def test_klines_that_match_their_trades_pass_the_cross_check(
    fake: FakeArchive, tmp_path: Path
) -> None:
    minute = [("1.50", "2", True), ("1.25", "1", False)]
    fake.put("d", "AAAUSDT", "2026-10-01", [_kline_of(OCT_MS, minute)])
    fake.put_trades("AAAUSDT", "2026-10-01", [
        f"1,1.50,2,3.0,{OCT_MS + 1},false", f"2,1.25,1,1.25,{OCT_MS + 2},true",
    ])  # fmt: skip
    remote = DirectoryRemote(tmp_path / "remote")
    fake.publish(remote, utc(2026, 11, 9))
    cc = u1.crosscheck(CROSSCHECK_CFG, remote, LIMITS, tmp_path / "w", utc(2026, 11, 10), seed=1)
    assert (cc.days, cc.minutes, cc.passed) == (1, 1, True)
    assert cc.report == "quality/u1-crosscheck/20261110T000000Z.json"
    report = json.loads((remote.root / cc.report).read_text())
    assert report["totals"]["differing"] == 0
    assert report["days"][0]["symbol"] == "AAAUSDT"


def test_klines_that_differ_from_their_trades_fail_the_cross_check(
    fake: FakeArchive, tmp_path: Path
) -> None:
    fake.put("d", "AAAUSDT", "2026-10-01", [_kline_of(OCT_MS, [("1.50", "2", True)])])
    fake.put_trades("AAAUSDT", "2026-10-01", [f"1,1.50,3,4.5,{OCT_MS + 1},false"])  # 3, not 2
    remote = DirectoryRemote(tmp_path / "remote")
    fake.publish(remote, utc(2026, 11, 9))
    cc = u1.crosscheck(CROSSCHECK_CFG, remote, LIMITS, tmp_path / "w", utc(2026, 11, 10), seed=1)
    assert not cc.passed
    report = json.loads((remote.root / cc.report).read_text())
    assert (report["totals"]["volume"], report["share_differing"]) == (1, 1.0)
