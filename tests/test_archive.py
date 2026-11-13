"""Archive runs against a fake archive (hand-written listings and files) and a
local stand-in for the remote store."""

import hashlib
import io
import random
import re
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from finresearch import archive
from finresearch.core.time import utc
from finresearch.data.manifest import ManifestEntry, read_increment, replay
from finresearch.data.store import DirectoryRemote, Limits
from finresearch.registry.models import SOURCE_ID
from finresearch.sources.base import SourceError
from tests.sources.conftest import FakeServer, install

REPO = Path(__file__).resolve().parents[1]
LIMITS = Limits(
    max_files_per_commit=50, max_entries_per_folder=9000, retries=0, retry_wait_seconds=0
)
CFG: dict[str, Any] = {
    "listing": {
        "workers": 2,
        "describe_workers": 2,
        "describe_max": 100,
        "max_shown": 5,
        "max_skipped": 1,
    },
    "verify": {"max_sample": 4, "new_share": 0.5},
    "datasets": {
        "binance.um.metrics.daily": {
            "exchange": "binance",
            "root": "data/futures/um/daily/metrics/",
            "sample": 3,
            "enabled": True,
        },
        "bybit.trades.daily": {"exchange": "bybit", "root": "trading/", "enabled": False},
    },
}
ROOT = "data/futures/um/daily/metrics/"
NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name.removesuffix(".zip") + ".csv", f"made-up rows of {name}\n")
    return buf.getvalue()


class FakeArchive:
    """A Binance-style archive: an S3 index plus the files and their checksum files."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}  # key -> bytes

    def put(self, symbol: str, day: str) -> None:
        name = f"{symbol}-metrics-{day}.zip"
        key = f"{ROOT}{symbol}/{name}"
        self.files[key] = _zip(name)
        self.files[key + ".CHECKSUM"] = (
            f"{hashlib.sha256(self.files[key]).hexdigest()}  {name}\n".encode()
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "s3-ap-northeast-1.amazonaws.com":
            return httpx2.Response(200, content=self._index(request.url.params["prefix"]))
        key = request.url.path.removeprefix("/")
        etag = hashlib.md5(self.files[key], usedforsecurity=False).hexdigest()
        return httpx2.Response(200, content=self.files[key], headers={"ETag": f'"{etag}"'})

    def _index(self, prefix: str) -> bytes:
        below = sorted(k for k in self.files if k.startswith(prefix))
        folders = sorted(
            {
                prefix + k.removeprefix(prefix).split("/")[0] + "/"
                for k in below
                if "/" in k.removeprefix(prefix)
            }
        )
        keys = [k for k in below if "/" not in k.removeprefix(prefix)]
        parts = [f'<ListBucketResult xmlns="{NS}"><IsTruncated>false</IsTruncated>']
        for k in keys:
            md5 = hashlib.md5(self.files[k], usedforsecurity=False).hexdigest()
            parts.append(
                f"<Contents><Key>{k}</Key><LastModified>2026-11-12T01:02:03.000Z</LastModified>"
                f"<ETag>&quot;{md5}&quot;</ETag><Size>{len(self.files[k])}</Size></Contents>"
            )
        parts += [f"<CommonPrefixes><Prefix>{f}</Prefix></CommonPrefixes>" for f in folders]
        return ("".join(parts) + "</ListBucketResult>").encode()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    return install(monkeypatch)


@pytest.fixture
def fake(server: FakeServer) -> FakeArchive:
    a = FakeArchive()
    server.handler = a
    return a


def _run(remote: DirectoryRemote, tmp: Path, now: datetime) -> archive.RunResult:
    return archive.run(
        "binance.um.metrics.daily", CFG, remote, LIMITS, tmp / now.isoformat(), now, seed=7
    )


def test_plan_lists_the_enabled_datasets() -> None:
    assert archive.plan(CFG) == ["binance.um.metrics.daily"]


def test_a_disabled_or_unknown_dataset_is_refused(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    for name in ("bybit.trades.daily", "nope.x"):
        with pytest.raises(archive.ArchiveError, match="not an enabled dataset"):
            archive.run(name, CFG, remote, LIMITS, tmp_path, utc(2026, 11, 15), 1)


def test_runs_store_increments_that_replay_to_each_listing(
    fake: FakeArchive, tmp_path: Path
) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    for symbol in ("AAAUSDT", "BBBUSDT"):
        for day in ("2026-11-09", "2026-11-10"):
            fake.put(symbol, day)

    first = _run(remote, tmp_path, utc(2026, 11, 15, 3, 37))
    assert (first.symbols, first.files, first.counts) == (
        2,
        4,
        {"added": 4, "changed": 0, "removed": 0},
    )
    assert first.increment == "manifests/binance.um.metrics.daily/20261115T033700Z.parquet"
    assert first.pushed.added == [first.increment]
    assert len(first.checks) == 3  # sample = 3, all of them new
    assert first.ok

    fake.put("AAAUSDT", "2026-11-11")  # a new day
    del fake.files[f"{ROOT}BBBUSDT/BBBUSDT-metrics-2026-11-09.zip"]  # a file vanishes
    second = _run(remote, tmp_path, utc(2026, 11, 22, 3, 37))
    assert second.counts == {"added": 1, "changed": 0, "removed": 1}
    assert second.examples == {
        "removed": [f"https://data.binance.vision/{ROOT}BBBUSDT/BBBUSDT-metrics-2026-11-09.zip"]
    }
    assert second.ok

    increments = sorted(remote.list("manifests"))
    assert [p.rsplit("/", 1)[1] for p in increments] == [
        "20261115T033700Z.parquet",
        "20261122T033700Z.parquet",
    ]
    state = replay(read_increment(remote.root / p) for p in increments)
    assert sorted(e.period for e in state.values()) == [
        "2026-11-09",
        "2026-11-10",
        "2026-11-10",
        "2026-11-11",
    ]


def test_a_file_replaced_under_the_same_name_is_caught(fake: FakeArchive, tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    fake.put("AAAUSDT", "2026-11-10")
    _run(remote, tmp_path, utc(2026, 11, 15))
    key = f"{ROOT}AAAUSDT/AAAUSDT-metrics-2026-11-10.zip"
    fake.files[key] = _zip("AAAUSDT-metrics-2026-11-10.zip") + b"\0"  # new bytes, old checksum file
    result = _run(remote, tmp_path, utc(2026, 11, 22))
    assert result.counts["changed"] == 1
    assert not result.ok
    assert any("SHA-256" in p for p in result.checks[0].problems)


def test_names_that_do_not_fit_are_reported_up_to_a_limit(
    fake: FakeArchive, tmp_path: Path
) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    fake.put("AAAUSDT", "2026-11-10")
    fake.put("AAAUSDT", "2026-11-11")
    del fake.files[f"{ROOT}AAAUSDT/AAAUSDT-metrics-2026-11-11.zip.CHECKSUM"]
    fake.files[f"{ROOT}AAAUSDT/AAAUSDT-metrics-1993.zip"] = b"?"
    result = _run(remote, tmp_path, utc(2026, 11, 15))
    assert result.skipped == [f"{ROOT}AAAUSDT/AAAUSDT-metrics-1993.zip"]
    assert result.files == 2  # the stray is not an entry
    assert result.no_checksum == 1

    fake.files[f"{ROOT}AAAUSDT/AAAUSDT-metrics-2026-W46.zip"] = b"?"  # a new naming scheme
    with pytest.raises(archive.ArchiveError, match="2 names do not fit"):
        _run(remote, tmp_path, utc(2026, 11, 22))
    assert len(remote.list("manifests")) == 1  # the second listing stored nothing


def test_a_listing_failure_stores_nothing(server: FakeServer, tmp_path: Path) -> None:
    server.handler = lambda _r: httpx2.Response(403)
    remote = DirectoryRemote(tmp_path / "remote")
    with pytest.raises(SourceError):
        _run(remote, tmp_path, utc(2026, 11, 15))
    assert remote.list("manifests") == {}


def _entry(n: int) -> ManifestEntry:
    return ManifestEntry("d", "S", "2026-11-10", f"https://x.invalid/{n:02d}.zip")


def test_pick_takes_about_half_from_the_new_files_and_repeats_with_the_seed() -> None:
    entries = [_entry(n) for n in range(20)]
    new = entries[15:]
    picked = archive.pick(entries, new, 6, 0.5, random.Random(1))
    assert len({e.url for e in picked}) == 6
    assert sum(e in new for e in picked) >= 3
    assert archive.pick(entries, new, 6, 0.5, random.Random(1)) == picked


def test_pick_never_asks_for_more_than_there_is() -> None:
    entries = [_entry(n) for n in range(2)]
    assert len(archive.pick(entries, [], 5, 0.5, random.Random(1))) == 2
    assert archive.pick([], [], 5, 0.5, random.Random(1)) == []


class _NoContentCheck:
    @staticmethod
    def check(entry: ManifestEntry, content: bytes) -> list[str]:
        return []


@pytest.mark.parametrize(
    ("etag_header", "manifest", "problem"),
    [
        ('"{md5}"', ManifestEntry("d", "S", "p", "https://x.invalid/f", 5, "{md5}"), None),
        ('W/"{md5}"', ManifestEntry("d", "S", "p", "https://x.invalid/f"), None),
        ('"abc-2"', ManifestEntry("d", "S", "p", "https://x.invalid/f"), None),  # multipart: no MD5
        (
            '"{md5}"',
            ManifestEntry("d", "S", "p", "https://x.invalid/f", 6),
            "5 bytes, the manifest lists 6",
        ),
        ('"{md5}"', ManifestEntry("d", "S", "p", "https://x.invalid/f", None, "old"), "ETag"),
        ('"{zeros}"', ManifestEntry("d", "S", "p", "https://x.invalid/f"), "MD5 of the bytes"),
    ],
)
def test_verify_compares_size_etag_and_md5(
    server: FakeServer, etag_header: str, manifest: ManifestEntry, problem: str | None
) -> None:
    md5 = hashlib.md5(b"hello", usedforsecurity=False).hexdigest()
    server.handler = lambda _r: httpx2.Response(
        200, content=b"hello", headers={"ETag": etag_header.format(md5=md5, zeros="0" * 32)}
    )
    if manifest.etag == "{md5}":
        manifest = ManifestEntry("d", "S", "p", manifest.url, manifest.size, md5)
    check = archive.verify(manifest, _NoContentCheck())  # pyright: ignore[reportArgumentType]
    assert check.sha256 == hashlib.sha256(b"hello").hexdigest()
    if problem is None:
        assert check.ok
    else:
        (found,) = check.problems
        assert problem in found


def test_the_configured_datasets_are_registered_sources() -> None:
    cfg = tomllib.loads((REPO / "config/archives.toml").read_text(encoding="utf-8"))
    assert archive.LISTERS.keys() == {"binance", "bybit"}
    for name, spec in cfg["datasets"].items():
        assert SOURCE_ID.fullmatch(name), name
        assert spec["exchange"] in archive.LISTERS
        assert (REPO / "registry/sources" / f"{name}.toml").is_file(), name
        assert spec.get("sample", 0) <= cfg["verify"]["max_sample"]
        if spec["exchange"] == "binance":
            assert re.fullmatch(r"data/futures/um/(daily|monthly)/\w+/", spec["root"])


def test_the_archive_workflow_is_kept_alive() -> None:
    collect = tomllib.loads((REPO / "config/collect.toml").read_text(encoding="utf-8"))
    assert ".github/workflows/archive.yml" in collect["monitor"]["scheduled_workflows"]
    assert (REPO / ".github/workflows/archive.yml").is_file()


class _Asking:
    """A lister whose listing gives names only, and that counts the files it is asked about."""

    OFFICIAL_CHECKSUMS = False

    def __init__(self, refuse_from: int | None = None) -> None:
        self.asked: list[str] = []
        self.refuse_from = refuse_from

    def describe(self, entry: ManifestEntry) -> ManifestEntry:
        self.asked.append(entry.url)
        if self.refuse_from is not None and entry.url >= _entry(self.refuse_from).url:
            raise SourceError(f"HEAD {entry.url}: HTTP 403")
        return ManifestEntry(
            entry.dataset, entry.symbol, entry.period, entry.url, 7, "e", utc(2026, 11, 2)
        )


def test_only_files_never_described_are_asked_about() -> None:
    named = [_entry(n) for n in range(3)]
    before = {
        named[0].url: ManifestEntry(
            "d", "S", "2026-11-10", named[0].url, 5, "old", utc(2026, 11, 1)
        )
    }
    lister = _Asking()
    d = archive.describe_all(named, before, lister, workers=2, limit=100)  # pyright: ignore[reportArgumentType]
    assert (d.asked, d.left, d.error) == (2, 0, None)
    assert sorted(lister.asked) == [named[1].url, named[2].url]
    assert [(e.size, e.etag) for e in d.entries] == [(5, "old"), (7, "e"), (7, "e")]
    described = ManifestEntry("d", "S", "2026-11-10", "https://x.invalid/b.zip", 9, "x")
    again = archive.describe_all([described], {}, lister, workers=2, limit=100)  # pyright: ignore[reportArgumentType]
    assert (again.entries, again.asked) == ([described], 0)


def test_at_most_the_limit_is_asked_about_and_the_rest_waits() -> None:
    named = [_entry(n) for n in range(5)]
    d = archive.describe_all(named, {}, _Asking(), workers=2, limit=3)  # pyright: ignore[reportArgumentType]
    assert (d.asked, d.left, d.error) == (3, 2, None)
    assert [e.size for e in d.entries] == [7, 7, 7, None, None]  # in URL order, the first three


def test_a_refusal_stops_the_asking_and_keeps_what_was_learned() -> None:
    named = [_entry(n) for n in range(80)]  # chunks of 2 workers * 16 = 32
    lister = _Asking(refuse_from=35)
    d = archive.describe_all(named, {}, lister, workers=2, limit=100)  # pyright: ignore[reportArgumentType]
    assert d.error is not None
    assert d.error.endswith("HTTP 403")
    assert (d.asked, d.left, len(d.entries)) == (35, 45, 80)
    assert len(lister.asked) == 64  # the refusal came in the second chunk; no third was sent
