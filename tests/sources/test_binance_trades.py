"""Binance trade files written by hand in the archive's format, with made-up trades."""

import io
import zipfile

import pytest

from finresearch.core.time import utc
from finresearch.data.manifest import ManifestEntry
from finresearch.sources.binance_trades import TradeError, parse_trades

# 2026-11-11 00:00 UTC is 1,794,355,200 seconds after the epoch (20768 days).
MS = 1_794_355_200_000
NAME = "AAAUSDT-trades-2026-11-11.zip"
ENTRY = ManifestEntry("t", "AAAUSDT", "2026-11-11", f"https://x.invalid/{NAME}")
HEADER = "id,price,qty,quote_qty,time,is_buyer_maker"


def _zip(*rows: str, header: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            NAME.removesuffix(".zip") + ".csv",
            "\n".join([HEADER, *rows] if header else rows) + "\n",
        )
    return buf.getvalue()


ROWS = [  # out of order on purpose; ids 11 and 12 share a millisecond
    f"12,10.5,2,21.0,{MS + 500},true",
    f"10,10.0,1.5,15.0,{MS + 100},false",
    f"11,11.0,1,11.0,{MS + 500},false",
    f"13,1.7458208E7,1,17458208,{MS + 60_000},true",
]


@pytest.mark.parametrize("header", [True, False])
def test_trades_become_bars_in_time_then_id_order(header: bool) -> None:
    first, second = parse_trades(
        _zip(*ROWS, header=header), ENTRY, utc(2026, 11, 12)
    ).table.to_pylist()
    # minute 1, in order: id 10 at 10.0, id 11 at 11.0, id 12 at 10.5
    assert (first["open"], first["high"], first["low"], first["close"], first["open_scale"]) == (
        100,
        110,
        100,
        105,
        1,
    )
    assert (first["volume"], first["volume_scale"], first["trades"]) == (45, 1, 3)
    # taker bought where is_buyer_maker is false: ids 10 and 11, 1.5 + 1 = 2.5
    assert (first["taker_buy_volume"], first["taker_buy_quote_volume"]) == (
        25,
        2600,
    )  # 15.0 + 11.0 at scale 2
    assert (second["open"], second["open_scale"]) == (174582080, 1)  # 17,458,208.0 read exactly


def test_a_bad_maker_flag_is_refused() -> None:
    with pytest.raises(TradeError, match="neither true nor false"):
        parse_trades(_zip(f"1,1.0,1,1,{MS},maybe"), ENTRY, utc(2026, 11, 12))
    with pytest.raises(TradeError, match="no trades"):
        parse_trades(_zip(), ENTRY, utc(2026, 11, 12))
