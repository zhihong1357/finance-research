"""The directory pages and day files below are written by hand in Bybit's format,
with made-up symbols, trades and IDs."""

import gzip

import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.data.manifest import ManifestEntry
from finresearch.sources import bybit_archive as b
from tests.sources.conftest import FakeServer

SPEC = {"root": "trading/"}
BASE = "https://public.bybit.com/trading/"


def _page(*links: str) -> bytes:
    items = "".join(f'<li><a href="{x}">{x}</a></li>' for x in links)
    return f"<html><body><h1>Directory listing</h1><ul>{items}</ul></body></html>".encode()


def test_symbols_are_the_folders(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(200, content=_page("BBBUSDT/", "AAAUSD/", "x.txt"))
    assert b.symbols(SPEC) == ["AAAUSD", "BBBUSDT"]
    assert str(server.requests[0].url) == BASE


def test_entries_keep_the_folder_and_read_the_day_from_the_name(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(
        200,
        content=_page(
            "AAAUSDT2021-12-17.csv.gz",  # a renamed folder keeps the old contract's files
            "AAAUSDT2021-12-18_v2.csv.gz",  # a reissued day
            "notes.txt",
        ),
    )
    found, skipped = b.entries("bybit.trades.daily", SPEC, "AAAOLDUSDT")
    folder = f"{BASE}AAAOLDUSDT/"
    assert str(server.requests[0].url) == folder
    assert found == [
        ManifestEntry(
            "bybit.trades.daily", "AAAOLDUSDT", "2021-12-17", f"{folder}AAAUSDT2021-12-17.csv.gz"
        ),
        ManifestEntry(
            "bybit.trades.daily", "AAAOLDUSDT", "2021-12-18", f"{folder}AAAUSDT2021-12-18_v2.csv.gz"
        ),
    ]
    assert skipped == [f"{folder}notes.txt"]


@pytest.mark.parametrize(
    ("name", "symbol", "day"),
    [
        ("BTCUSDU212021-07-26_v2.csv.gz", "BTCUSDU21", "2021-07-26"),  # ends in digits
        ("BTC-01DEC232023-11-10.csv.gz", "BTC-01DEC23", "2023-11-10"),  # a dated contract
        ("WC_AAA_BBB_USDT-28JUN262026-06-23.csv.gz", "WC_AAA_BBB_USDT-28JUN26", "2026-06-23"),
    ],
)
def test_the_symbol_is_whatever_comes_before_the_day(name: str, symbol: str, day: str) -> None:
    m = b._FILE.fullmatch(name)  # pyright: ignore[reportPrivateUsage]
    assert m is not None
    assert (m["symbol"], m["day"]) == (symbol, day)


def test_a_dated_contract_file_passes() -> None:
    entry = ManifestEntry(
        "d", "BTC-01DEC23", "2026-11-12", f"{BASE}BTC-01DEC23/BTC-01DEC232026-11-12.csv.gz"
    )
    assert b.check(entry, _file(_row(DAY + 5, "BTC-01DEC23"))) == []


HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,"
    "foreignNotional"
)
# 2026-11-12 00:00:00 UTC is 1794441600 seconds after the epoch (20769 days * 86400).
DAY = 1794441600
ENTRY = ManifestEntry(
    "bybit.trades.daily", "AAAUSDT", "2026-11-12", f"{BASE}AAAUSDT/AAAUSDT2026-11-12.csv.gz"
)


def _file(*rows: str, header: str = HEADER) -> bytes:
    return gzip.compress(("\n".join([header, *rows]) + "\n").encode())


def _row(ts: float, symbol: str = "AAAUSDT") -> str:
    return f"{ts},{symbol},Buy,1.5,0.25,PlusTick,id-1,3.75e+08,1.5,0.375"


def test_a_day_of_trades_passes() -> None:
    assert b.check(ENTRY, _file(_row(DAY + 0.0834), _row(DAY + 86399.9))) == []


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (_file(_row(DAY - 0.5), _row(DAY + 10)), "1 of 2 rows fall outside 2026-11-12"),
        (_file(_row(DAY + 86400.0)), "1 of 1 rows fall outside 2026-11-12"),
        (_file(_row(DAY + 1, "BBBUSDT")), "1 of 1 rows are not AAAUSDT"),
        (_file(), "no trades in the file"),
        (_file(_row(DAY), header="time,sym"), "unexpected header"),
        (b"not gzip", "cannot read it as a gzip CSV: BadGzipFile"),
        (_file("abc,AAAUSDT"), "cannot read it as a gzip CSV: ValueError"),
        (_file(f"{DAY}"), "cannot read it as a gzip CSV: IndexError"),
    ],
)
def test_bad_day_files_are_reported(content: bytes, problem: str) -> None:
    (found,) = b.check(ENTRY, content)
    assert found.startswith(problem)


def test_describe_asks_the_server_for_size_etag_and_publication(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(
        200,
        headers={
            "Content-Length": "1240124",
            "ETag": '"1e756dc7ee73cff4a287673f33912fa6"',
            "Last-Modified": "Fri, 13 Nov 2026 01:08:08 GMT",
        },
    )
    described = b.describe(ENTRY)
    assert server.requests[0].method == "HEAD"
    assert (described.size, described.etag) == (1240124, "1e756dc7ee73cff4a287673f33912fa6")
    assert described.last_modified == utc(2026, 11, 13, 1, 8, 8)
    assert (described.url, described.symbol, described.period) == (
        ENTRY.url,
        ENTRY.symbol,
        ENTRY.period,
    )
