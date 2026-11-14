"""Mark price kline files written by hand in Binance's format, with made-up prices."""

import pytest

from finresearch.core.time import utc
from finresearch.data.manifest import ManifestEntry
from finresearch.sources.binance_mark import MarkFileError, parse_mark

HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)
# 2026-11-11 00:00 UTC is 1,794,355,200 seconds after the epoch (20768 days).
MS = 1_794_355_200_000


def _row(ms: int, close: str, count: int = 60) -> str:
    return f"{ms},100.0,101.0,99.0,{close},0,{ms + 59_999},0,{count},0,0,0"


ROWS = [
    _row(MS + 60_000, "100.25000000", 0),  # no samples: the previous close carried forward
    _row(MS, "100.25000000"),  # out of order
    _row(MS + 120_000, "100.5", 58),
]
URL = "https://x.invalid/data/futures/um/daily/markPriceKlines/AAAUSDT/1m/AAAUSDT-1m-2026-11-11.zip"
DAY = ManifestEntry("binance.um.mark_1m.daily", "AAAUSDT", "2026-11-11", URL)
PUBLISHED = utc(2026, 11, 12, 1, 8)


def _csv(*rows: str, header: bool = True) -> bytes:
    return ("\n".join([HEADER, *rows] if header else rows) + "\n").encode()


@pytest.mark.parametrize("header", [True, False])
def test_a_mark_file_keeps_the_close_and_the_sample_count(header: bool) -> None:
    t = parse_mark(_csv(*ROWS, header=header), DAY, PUBLISHED)
    rows = t.table.to_pylist()
    assert [r["event_time"] for r in rows] == [
        utc(2026, 11, 11, 0, 0),
        utc(2026, 11, 11, 0, 1),
        utc(2026, 11, 11, 0, 2),
    ]
    # 100.5 at the column's eight decimals is 10,050,000,000.
    assert [(r["close"], r["close_scale"], r["samples"]) for r in rows] == [
        (10_025_000_000, 8, 60),
        (10_025_000_000, 8, 0),
        (10_050_000_000, 8, 58),
    ]
    assert {(r["exchange"], r["symbol"], r["ingested_at"]) for r in rows} == {
        ("binance", "AAAUSDT", PUBLISHED)
    }
    assert sorted(t.table.column_names) == [
        "available_at", "close", "close_scale", "event_time", "exchange", "ingested_at",
        "samples", "symbol",
    ]  # fmt: skip


def test_a_monthly_file_covers_its_month() -> None:
    url = URL.replace("daily", "monthly").replace("2026-11-11", "2026-11")
    month = ManifestEntry("binance.um.mark_1m.monthly", "AAAUSDT", "2026-11", url)
    t = parse_mark(_csv(_row(MS, "1"), _row(MS + 19 * 86_400_000, "2")), month, utc(2026, 12, 2))
    assert t.num_rows == 2
    with pytest.raises(MarkFileError, match="outside 2026-11"):
        parse_mark(_csv(_row(MS + 20 * 86_400_000, "2")), month, utc(2026, 12, 2))


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row(MS - 60_000, "1")], "outside 2026-11-11"),
        ([_row(MS, "1").replace(f"{MS + 59_999}", f"{MS + 60_000}")], "59,999"),
        ([_row(MS, "1"), _row(MS, "2")], "repeated or out of order"),
        ([_row(MS + 1, "1")], "whole minute"),
        ([_row(MS, "one")], "not a plain decimal"),
        ([f"{MS},1,1,1,1"], "CSV"),
    ],
)
def test_bad_files_are_refused(rows: list[str], message: str) -> None:
    with pytest.raises(MarkFileError, match=message):
        parse_mark(_csv(*rows), DAY, PUBLISHED)
