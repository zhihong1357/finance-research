"""Kline files written by hand in Binance's format, with made-up prices and volumes."""

import io
import zipfile

import pytest

from finresearch.core.time import utc
from finresearch.data.manifest import ManifestEntry
from finresearch.sources.binance_klines import KlineError, csv_from_zip, parse_klines

HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)
# 2026-11-11 00:00 UTC is 1,794,355,200 seconds after the epoch (20768 days).
MS = 1_794_355_200_000
ROWS = [
    f"{MS + 60_000},101.5,101.5,100.25,100.5,0.500,{MS + 119_999},50.5,2,0.000,0.0,0",
    f"{MS},100.0,102.0,99.5,101.5,3.000,{MS + 59_999},302.5,7,2.000,201.0,0",  # out of order
]
URL = "https://x.invalid/data/futures/um/daily/klines/AAAUSDT/1m/AAAUSDT-1m-2026-11-11.zip"
DAY = ManifestEntry("binance.um.klines_1m.daily", "AAAUSDT", "2026-11-11", URL)
PUBLISHED = utc(2026, 11, 12, 1, 8)


def _csv(*rows: str, header: bool = True) -> bytes:
    return ("\n".join([HEADER, *rows] if header else rows) + "\n").encode()


@pytest.mark.parametrize("header", [True, False])
def test_klines_become_bars_with_or_without_a_header(header: bool) -> None:
    t = parse_klines(_csv(*ROWS, header=header), DAY, PUBLISHED)
    rows = t.table.to_pylist()
    assert [r["event_time"] for r in rows] == [utc(2026, 11, 11, 0, 0), utc(2026, 11, 11, 0, 1)]
    first = rows[0]
    assert (first["exchange"], first["symbol"], first["trades"]) == ("binance", "AAAUSDT", 7)
    assert (first["open"], first["open_scale"]) == (1000, 1)
    assert (first["low"], first["low_scale"]) == (9950, 2)
    assert (first["volume"], first["volume_scale"]) == (3000, 3)
    assert (first["taker_buy_quote_volume"], first["taker_buy_quote_volume_scale"]) == (2010, 1)
    assert first["ingested_at"] == PUBLISHED


def test_a_monthly_file_covers_its_month() -> None:
    entry = ManifestEntry("d", "AAAUSDT", "2026-11", URL.replace("2026-11-11", "2026-11"))
    assert parse_klines(_csv(*ROWS), entry, utc(2026, 12, 1, 2)).num_rows == 2
    december = f"{1_796_083_200_000},1,1,1,1,0,{1_796_083_200_000 + 59_999},0,0,0,0,0"  # 12-01
    with pytest.raises(KlineError, match="outside 2026-11"):
        parse_klines(_csv(december), entry, utc(2026, 12, 2))


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (f"{MS - 60_000},1,1,1,1,0,{MS - 1},0,0,0,0,0", "outside 2026-11-11"),
        (f"{MS},1,1,1,1,0,{MS + 60_000},0,0,0,0,0", "close_time"),
        (f"{MS},1,1,1,1,1e3,{MS + 59_999},0,0,0,0,0", "not a plain decimal"),
        (f"{MS + 30_000},1,1,1,1,0,{MS + 89_999},0,0,0,0,0", "whole minute"),
    ],
)
def test_bad_rows_are_refused(row: str, message: str) -> None:
    with pytest.raises(KlineError, match=message):
        parse_klines(_csv(row), DAY, PUBLISHED)


def test_a_repeated_minute_is_refused() -> None:
    with pytest.raises(KlineError, match="repeated"):
        parse_klines(_csv(ROWS[1], ROWS[1]), DAY, PUBLISHED)


def test_the_csv_comes_out_of_its_zip() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AAAUSDT-1m-2026-11-11.csv", "x")
    assert csv_from_zip(buf.getvalue(), DAY) == b"x"
    with pytest.raises(KlineError, match=r"holds \[.AAAUSDT-1m-2026-11-11\.csv.\], expected B-1m"):
        csv_from_zip(
            buf.getvalue(), ManifestEntry("d", "B", "2026-11-11", URL.replace("AAAUSDT-1m", "B-1m"))
        )
    with pytest.raises(KlineError, match="not a valid zip"):
        csv_from_zip(b"nope", DAY)
