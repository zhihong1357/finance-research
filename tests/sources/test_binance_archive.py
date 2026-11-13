"""The S3 index answers below are written by hand in AWS's format, with made-up
symbols, sizes and ETags; the zips are built in the test."""

import hashlib
import io
import zipfile
from collections.abc import Sequence

import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.data.manifest import ManifestEntry
from finresearch.sources import binance_archive as b
from tests.sources.conftest import FakeServer

ROOT = "data/futures/um/daily/metrics/"
SPEC = {"root": ROOT}
HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
)
LISTED = utc(2026, 11, 12, 1, 2, 3)


def _xml(
    *, folders: Sequence[str] = (), keys: Sequence[tuple[str, int]] = (), next_marker: str = ""
) -> bytes:
    """An index page; a key's ETag is its size written as 32 hex digits."""
    parts = [HEAD, f"<IsTruncated>{'true' if next_marker else 'false'}</IsTruncated>"]
    if next_marker:
        parts.append(f"<NextMarker>{next_marker}</NextMarker>")
    parts += [
        f"<Contents><Key>{k}</Key><LastModified>2026-11-12T01:02:03.000Z</LastModified>"
        f"<ETag>&quot;{n:032x}&quot;</ETag><Size>{n}</Size></Contents>"
        for k, n in keys
    ]
    parts += [f"<CommonPrefixes><Prefix>{f}</Prefix></CommonPrefixes>" for f in folders]
    return ("".join(parts) + "</ListBucketResult>").encode()


def test_a_page_gives_folders_keys_and_the_next_marker() -> None:
    page = b.parse_page(
        _xml(folders=[f"{ROOT}AAAUSDT/"], keys=[("a.zip", 7)], next_marker=f"{ROOT}AAAUSDT/")
    )
    assert page.folders == [f"{ROOT}AAAUSDT/"]
    assert page.keys == [b.Key("a.zip", 7, f"{7:032x}", LISTED)]
    assert page.next_marker == f"{ROOT}AAAUSDT/"


def test_the_last_page_has_no_marker() -> None:
    assert b.parse_page(_xml(keys=[("a.zip", 1)])).next_marker is None


def test_without_next_marker_the_last_name_continues_a_truncated_listing() -> None:
    content = _xml(keys=[("a.zip", 1), ("c.zip", 1)]).replace(
        b"false</IsTruncated>", b"true</IsTruncated>"
    )
    assert b.parse_page(content).next_marker == "c.zip"


def test_symbols_follow_the_markers_across_pages(server: FakeServer) -> None:
    def index(request: httpx2.Request) -> httpx2.Response:
        q = request.url.params
        assert (q["prefix"], q["delimiter"]) == (ROOT, "/")
        if q["marker"] == "":
            return httpx2.Response(
                200, content=_xml(folders=[f"{ROOT}BBBUSDT/"], next_marker=f"{ROOT}BBBUSDT/")
            )
        assert q["marker"] == f"{ROOT}BBBUSDT/"
        return httpx2.Response(200, content=_xml(folders=[f"{ROOT}AAAUSDT/", f"{ROOT}币安USDT/"]))

    server.handler = index
    assert b.symbols(SPEC) == ["AAAUSDT", "BBBUSDT", "币安USDT"]
    assert len(server.requests) == 2


def test_entries_pair_each_zip_with_its_checksum_and_set_aside_strays(server: FakeServer) -> None:
    folder = f"{ROOT}AAAUSDT/"
    keys = [
        (f"{folder}AAAUSDT-metrics-2026-11-10.zip", 100),
        (f"{folder}AAAUSDT-metrics-2026-11-10.zip.CHECKSUM", 96),
        (f"{folder}AAAUSDT-metrics-2026-11-11.zip", 200),  # its checksum is missing
        (f"{folder}AAAUSDT-metrics-1993.zip", 5),
    ]

    def index(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["prefix"] == folder
        return httpx2.Response(200, content=_xml(keys=keys))

    server.handler = index
    found, skipped = b.entries("binance.um.metrics.daily", SPEC, "AAAUSDT")
    files = "https://data.binance.vision/" + folder
    assert found == [
        ManifestEntry(
            "binance.um.metrics.daily",
            "AAAUSDT",
            "2026-11-10",
            f"{files}AAAUSDT-metrics-2026-11-10.zip",
            100,
            f"{100:032x}",
            LISTED,
            f"{files}AAAUSDT-metrics-2026-11-10.zip.CHECKSUM",
        ),
        ManifestEntry(
            "binance.um.metrics.daily",
            "AAAUSDT",
            "2026-11-11",
            f"{files}AAAUSDT-metrics-2026-11-11.zip",
            200,
            f"{200:032x}",
            LISTED,
            None,
        ),
    ]
    assert skipped == [f"{folder}AAAUSDT-metrics-1993.zip"]


def test_kline_entries_look_in_the_interval_folder(server: FakeServer) -> None:
    spec = {"root": "data/futures/um/monthly/klines/", "sub": "1m/"}
    key = "data/futures/um/monthly/klines/AAAUSDT/1m/AAAUSDT-1m-2026-10.zip"

    def index(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["prefix"] == "data/futures/um/monthly/klines/AAAUSDT/1m/"
        return httpx2.Response(200, content=_xml(keys=[(key, 9)]))

    server.handler = index
    (entry,), _ = b.entries("binance.um.klines_1m.monthly", spec, "AAAUSDT")
    assert entry.period == "2026-10"


NAME = "AAAUSDT-metrics-2026-11-10.zip"
URL = f"https://data.binance.vision/{ROOT}AAAUSDT/{NAME}"


def _zip(member: str, text: str = "create_time,symbol\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, text)
    return buf.getvalue()


def _entry(checksum: bool = True) -> ManifestEntry:
    return ManifestEntry(
        "d", "AAAUSDT", "2026-11-10", URL, checksum_url=URL + ".CHECKSUM" if checksum else None
    )


def _serve_checksum(server: FakeServer, text: str) -> None:
    server.handler = lambda _r: httpx2.Response(200, content=text.encode())


def test_a_good_zip_passes(server: FakeServer) -> None:
    content = _zip("AAAUSDT-metrics-2026-11-10.csv")
    _serve_checksum(server, f"{hashlib.sha256(content).hexdigest()}  {NAME}\n")
    assert b.check(_entry(), content) == []
    assert str(server.requests[0].url) == URL + ".CHECKSUM"


@pytest.mark.parametrize(
    ("member", "checksum_text", "problem"),
    [
        ("AAAUSDT-metrics-2026-11-10.csv", f"{'0' * 64}  {NAME}", "SHA-256"),
        ("AAAUSDT-metrics-2026-11-10.csv", "{sha}  OTHER.zip", "does not name"),
        ("AAAUSDT-metrics-2026-11-09.csv", "{sha}  " + NAME, "expected"),
    ],
)
def test_bad_zips_are_reported(
    server: FakeServer, member: str, checksum_text: str, problem: str
) -> None:
    content = _zip(member)
    _serve_checksum(server, checksum_text.format(sha=hashlib.sha256(content).hexdigest()))
    (found,) = b.check(_entry(), content)
    assert problem in found


def test_a_missing_checksum_file_and_a_broken_zip_are_reported() -> None:
    problems = b.check(_entry(checksum=False), b"not a zip")
    assert problems[0] == "the archive has no official checksum file for it"
    assert problems[1].startswith("not a valid zip")


def test_a_corrupted_member_fails_its_crc() -> None:
    good = _zip("AAAUSDT-metrics-2026-11-10.csv", "x" * 50)
    bad = good.replace(b"x" * 50, b"y" + b"x" * 49)  # stored uncompressed, so the bytes are there
    assert any("fails its CRC" in p for p in b.check(_entry(checksum=False), bad))


def test_a_checksum_file_may_name_a_symbol_outside_ascii(server: FakeServer) -> None:
    name = "币安USDT-metrics-2026-11-10.zip"
    url = f"https://data.binance.vision/{ROOT}币安USDT/{name}"
    content = _zip("币安USDT-metrics-2026-11-10.csv")
    _serve_checksum(server, f"{hashlib.sha256(content).hexdigest()}  {name}\n")
    entry = ManifestEntry("d", "币安USDT", "2026-11-10", url, checksum_url=url + ".CHECKSUM")
    assert b.check(entry, content) == []
