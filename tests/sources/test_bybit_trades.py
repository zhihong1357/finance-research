"""Bybit day files written by hand in the archive's format, with made-up trades."""

import gzip

import pytest

from finresearch.core.time import utc
from finresearch.data import bars
from finresearch.data.manifest import ManifestEntry
from finresearch.sources.bybit_trades import TradeError, parse_trades

# 2026-11-12 00:00 UTC is 1,794,441,600 seconds after the epoch (20769 days).
DAY = 1_794_441_600
URL = "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2026-11-12.csv.gz"
ENTRY = ManifestEntry("bybit.trades.daily", "AAAUSDT", "2026-11-12", URL)
HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,"
    "foreignNotional,RPI"
)
PUBLISHED = utc(2026, 11, 13, 1, 8, 8)


def _row(t: str, side: str, size: str, price: str, symbol: str = "AAAUSDT") -> str:
    return f"{t},{symbol},{side},{size},{price},PlusTick,id,1e+08,{size},1,0"


def _gz(*rows: str, header: str = HEADER) -> bytes:
    return gzip.compress(("\n".join([header, *rows]) + "\n").encode())


TRADES = [  # oldest first; two minutes, a tie at 60.5
    _row(f"{DAY}.0834", "Sell", "2", "10.5"),
    _row(f"{DAY + 30}.5", "Buy", "1.5", "11"),
    _row(f"{DAY + 60}.5", "Buy", "3", "12.25"),
    _row(f"{DAY + 60}.5", "Sell", "1", "12"),
]


@pytest.mark.parametrize("newest_first", [False, True])
def test_trades_become_minute_bars_in_either_file_order(newest_first: bool) -> None:
    rows = TRADES[::-1] if newest_first else TRADES
    t = parse_trades(_gz(*rows), ENTRY, PUBLISHED)
    first, second = t.table.to_pylist()
    assert (first["exchange"], first["symbol"], first["event_time"]) == (
        "bybit",
        "AAAUSDT",
        utc(2026, 11, 12),
    )
    # minute 1: 10.5 then 11; prices at scale 2, sizes at scale 1
    assert (first["open"], first["high"], first["low"], first["close"], first["open_scale"]) == (
        1050,
        1100,
        1050,
        1100,
        2,
    )
    assert (first["volume"], first["volume_scale"], first["trades"]) == (35, 1, 2)
    # quote volume 2 * 10.5 + 1.5 * 11 = 37.5, exact at scale 3 (2 + 1)
    assert (first["quote_volume"], first["quote_volume_scale"]) == (37500, 3)
    assert (first["taker_buy_volume"], first["taker_buy_quote_volume"]) == (15, 16500)
    # minute 2: the tie at 60.5 keeps file order read oldest first: 12.25 opens, 12 closes
    assert (second["open"], second["close"], second["trades"]) == (1225, 1200, 2)
    assert second["event_time"] == utc(2026, 11, 12, 0, 1)
    assert second["ingested_at"] == PUBLISHED
    assert bars.problems(t) == []


def test_files_without_the_newer_column_read_the_same() -> None:
    old_header = HEADER.removesuffix(",RPI")
    rows = [r.removesuffix(",0") for r in TRADES]
    assert parse_trades(_gz(*rows, header=old_header), ENTRY, PUBLISHED).num_rows == 2


def test_sizes_in_scientific_notation_are_read_exactly() -> None:
    t = parse_trades(_gz(_row(f"{DAY}", "Buy", "1.0985e+06", "0.0125")), ENTRY, PUBLISHED)
    (bar,) = t.table.to_pylist()
    assert (bar["volume"], bar["volume_scale"]) == (1098500, 0)
    # 1,098,500 * 0.0125 = 13,731.25: exact at scale 4
    assert (bar["quote_volume"], bar["quote_volume_scale"]) == (137312500, 4)


def test_a_renamed_folder_keeps_the_folder_as_symbol() -> None:
    url = "https://public.bybit.com/trading/AAAOLDUSDT/AAAUSDT2026-11-12.csv.gz"
    t = parse_trades(_gz(*TRADES), ManifestEntry("d", "AAAOLDUSDT", "2026-11-12", url), PUBLISHED)
    assert set(t.table.column("symbol").to_pylist()) == {"AAAOLDUSDT"}


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row(f"{DAY - 1}", "Buy", "1", "1")], "outside 2026-11-12"),
        ([_row(f"{DAY + 86_400}", "Buy", "1", "1")], "outside 2026-11-12"),
        ([_row(f"{DAY}", "Buy", "1", "1", symbol="BBBUSDT")], "not AAAUSDT"),
        ([_row(f"{DAY}", "Hold", "1", "1")], "neither Buy nor Sell"),
        ([_row(f"{DAY}", "Buy", "1.2.3", "1")], "not a plain decimal"),
        ([_row(f"{DAY}.1234567", "Buy", "1", "1")], "finer than a microsecond"),
        ([], "no trades"),
    ],
)
def test_bad_files_are_refused(rows: list[str], message: str) -> None:
    with pytest.raises(TradeError, match=message):
        parse_trades(_gz(*rows), ENTRY, PUBLISHED)


def test_a_broken_gzip_or_missing_column_is_refused() -> None:
    with pytest.raises(TradeError, match="BadGzipFile"):
        parse_trades(b"not gzip", ENTRY, PUBLISHED)
    with pytest.raises(TradeError, match="CsvError"):
        parse_trades(_gz("1,AAAUSDT", header="timestamp,symbol"), ENTRY, PUBLISHED)
