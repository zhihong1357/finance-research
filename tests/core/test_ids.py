"""Dates and symbols here are illustrative, not reference data."""

from datetime import datetime

import pytest

from finresearch.core.ids import (
    IdentifierError,
    InstrumentId,
    Kind,
    Listing,
    ListingTable,
    SymbolKey,
    Venue,
)
from finresearch.core.time import NaiveDatetimeError, utc


def _id(text: str) -> InstrumentId:
    return InstrumentId.parse(text)


def test_symbol_key_round_trip() -> None:
    key = SymbolKey.parse("binance:perp:BTCUSDT")
    assert (key.venue, key.kind, key.symbol) == (Venue.BINANCE, Kind.PERP, "BTCUSDT")
    assert str(key) == "binance:perp:BTCUSDT"
    for text in (
        "deribit:option:BTC-27DEC26-100000-C",
        "okx:perp:BTC-USDT-SWAP",
        "cboe:option:SPX261120C06000000",
    ):
        assert str(SymbolKey.parse(text)) == text


@pytest.mark.parametrize(
    "text",
    [
        "binance:perp:btcusdt",
        "nasdaq:etf:QQQ",
        "binance:swap:BTCUSDT",
        "binance:perp",
        "binance:perp:BTC USDT",
    ],
)
def test_bad_symbol_keys(text: str) -> None:
    with pytest.raises(IdentifierError):
        SymbolKey.parse(text)


def test_instrument_id_round_trip() -> None:
    iid = _id("binance:perp:ABCUSDT@2021-03-01")
    assert str(iid) == "binance:perp:ABCUSDT@2021-03-01"
    assert iid.key == SymbolKey(Venue.BINANCE, Kind.PERP, "ABCUSDT")
    for bad in (
        "binance:perp:ABCUSDT",
        "binance:perp:ABCUSDT@2021-02-30",
        "binance:perp:abc@2021-03-01",
    ):
        with pytest.raises(IdentifierError):
            InstrumentId.parse(bad)


FIRST = Listing(
    _id("binance:perp:ABCUSDT@2021-03-01"), utc(2021, 3, 1, 8), utc(2022, 5, 13), "ABC", "USDT"
)
SECOND = Listing(_id("binance:perp:ABCUSDT@2023-01-10"), utc(2023, 1, 10, 8), None, "ABC", "USDT")


def test_reused_symbol_resolves_to_the_instrument_of_its_time() -> None:
    table = ListingTable([SECOND, FIRST])
    key = SymbolKey.parse("binance:perp:ABCUSDT")
    assert table.resolve(key, utc(2021, 6, 1)) == FIRST.instrument
    assert table.resolve(key, utc(2023, 2, 1)) == SECOND.instrument
    with pytest.raises(IdentifierError, match="named no instrument"):
        table.resolve(key, utc(2022, 7, 1))  # between delisting and relisting
    with pytest.raises(IdentifierError):
        table.resolve(key, utc(2022, 5, 13))  # the delisting instant itself is outside [start, end)


def test_contradictory_listing_tables_are_rejected() -> None:
    still_open = Listing(FIRST.instrument, FIRST.start, None, "ABC", "USDT")
    with pytest.raises(IdentifierError, match="overlaps"):
        ListingTable([still_open, SECOND])
    with pytest.raises(IdentifierError, match="listed twice"):
        ListingTable([FIRST, FIRST])


@pytest.mark.parametrize(
    ("start", "end", "base", "multiplier", "message"),
    [
        (utc(2021, 3, 2), None, "ABC", 1, "listing starts"),
        (utc(2021, 3, 1), utc(2021, 3, 1), "ABC", 1, "start before it ends"),
        (utc(2021, 3, 1), None, "abc", 1, "bad asset code"),
        (utc(2021, 3, 1), None, "ABC", 0, "positive integer"),
    ],
)
def test_bad_listings(
    start: datetime, end: datetime | None, base: str, multiplier: int, message: str
) -> None:
    with pytest.raises(IdentifierError, match=message):
        Listing(FIRST.instrument, start, end, base, "USDT", multiplier)


def test_listing_times_must_carry_a_time_zone() -> None:
    with pytest.raises(NaiveDatetimeError):
        Listing(FIRST.instrument, datetime(2021, 3, 1), None, "ABC", "USDT")  # noqa: DTZ001


def test_same_underlying_across_venues() -> None:
    binance = Listing(
        _id("binance:perp:1000XYZUSDT@2023-05-05"), utc(2023, 5, 5), None, "XYZ", "USDT", 1000
    )
    okx = Listing(_id("okx:perp:XYZ-USDT-SWAP@2023-05-06"), utc(2023, 5, 6), None, "XYZ", "USDT")
    spot = Listing(_id("binance:spot:XYZUSDT@2023-05-01"), utc(2023, 5, 1), None, "XYZ", "USDT")
    table = ListingTable([binance, okx, spot, FIRST, SECOND])
    assert table.same_underlying("XYZ", "USDT", Kind.PERP, utc(2023, 6, 1)) == [
        binance.instrument,
        okx.instrument,
    ]
    assert table.same_underlying("XYZ", "USDT", Kind.PERP, utc(2023, 5, 5, 12)) == [
        binance.instrument
    ]
    assert table.same_underlying("XYZ", "USDT", Kind.SPOT, utc(2023, 6, 1)) == [spot.instrument]
