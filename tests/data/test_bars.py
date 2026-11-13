from datetime import timedelta

import pyarrow as pa
import pytest

from finresearch.core.time import utc
from finresearch.data import bars
from finresearch.data.fixed import Fixed, FixedPointError, parse
from finresearch.data.table import PitTable, TableError

T0 = utc(2026, 11, 11)
US = 60_000_000  # one minute
PUBLISHED = utc(2026, 11, 12, 1, 8)


def _values(**overrides: list[str]) -> dict[str, Fixed]:
    base = {
        "open": ["100.0", "101.5"],
        "high": ["102.0", "101.5"],
        "low": ["99.5", "100.25"],
        "close": ["101.5", "100.5"],
        "volume": ["3.000", "0.500"],
        "quote_volume": ["302.5", "50.5"],
        "taker_buy_volume": ["2.000", "0.000"],
        "taker_buy_quote_volume": ["201.0", "0.0"],
    }
    base.update(overrides)
    return {k: parse(v) for k, v in base.items()}


def _bars(
    starts: list[int] | None = None,
    trades: list[int] | None = None,
    overrides: dict[str, list[str]] | None = None,
) -> PitTable:
    starts = (
        starts
        if starts is not None
        else [int(T0.timestamp()) * 1_000_000, int(T0.timestamp()) * 1_000_000 + US]
    )
    return bars.bars(
        "binance", "AAAUSDT", pa.array(starts, pa.int64()), _values(**(overrides or {})),
        pa.array(trades or [7, 2], pa.int64()), PUBLISHED,
    )  # fmt: skip


def test_a_bar_is_available_when_its_minute_ends() -> None:
    t = _bars()
    rows = t.table.to_pylist()
    assert t.name == "u1.bars_1m"
    assert t.keys == ("exchange", "symbol")
    assert rows[0]["event_time"] == T0
    assert rows[0]["available_at"] == T0 + timedelta(minutes=1)
    assert rows[1]["ingested_at"] == PUBLISHED
    assert (rows[1]["low"], rows[1]["low_scale"]) == (10025, 2)
    assert (rows[0]["volume"], rows[0]["volume_scale"], rows[0]["trades"]) == (3000, 3, 7)
    assert bars.problems(t) == []


@pytest.mark.parametrize(
    ("starts", "message"),
    [
        ([1, US], "whole minute"),
        ([US, US], "repeated or out of order"),
        ([2 * US, US], "repeated or out of order"),
    ],
)
def test_misaligned_or_repeated_minutes_are_refused(starts: list[int], message: str) -> None:
    with pytest.raises(bars.BarError, match=message):
        _bars(starts=starts)


def test_columns_must_line_up() -> None:
    with pytest.raises(bars.BarError, match="different lengths"):
        _bars(trades=[1, 2, 3])


def test_a_file_published_before_its_bars_ended_is_refused() -> None:
    late = int(PUBLISHED.timestamp()) * 1_000_000
    with pytest.raises(TableError, match="ingested before it was available"):
        _bars(starts=[late - US, late])


@pytest.mark.parametrize(
    ("overrides", "trades", "problem"),
    [
        ({"high": ["100.5", "101.5"]}, None, "high below open, close or low: 1 bar(s)"),
        ({"low": ["101.75", "100.25"]}, None, "low above open or close: 1 bar(s)"),
        ({"taker_buy_volume": ["3.001", "0.000"]}, None, "taker buy volume above volume"),
        (
            {"taker_buy_quote_volume": ["302.51", "0.0"]},
            None,
            "taker buy quote volume above quote volume",
        ),
        ({"taker_buy_volume": ["-1.000", "0.000"]}, None, "negative quantity"),
        ({}, [0, 2], "volume without trades"),
        ({"volume": ["3.000", "0"], "quote_volume": ["302.5", "0"]}, None, "trades without volume"),
    ],
)
def test_impossible_bars_are_reported(
    overrides: dict[str, list[str]], trades: list[int] | None, problem: str
) -> None:
    (found,) = bars.problems(_bars(trades=trades, overrides=overrides))
    assert found.startswith(problem)
    assert "AAAUSDT 2026-11-11 00:0" in found


def test_prices_at_different_scales_compare_exactly() -> None:
    # high "102" (scale 0) against open "101.99" (scale 2): fine; against "102.01": not.
    assert (
        bars.problems(_bars(overrides={"high": ["102", "102"], "open": ["101.99", "101.5"]})) == []
    )
    (found,) = bars.problems(_bars(overrides={"high": ["102", "102"], "open": ["102.01", "101.5"]}))
    assert found.startswith("high below open")


def test_trades_out_of_time_order_are_refused() -> None:
    with pytest.raises(bars.BarError, match="not in time order"):
        bars.from_trades(
            "bybit", "AAAUSDT", pa.array([2, 1], pa.int64()), parse(["1", "1"]), parse(["1", "1"]),
            pa.array([True, False]), PUBLISHED,
        )  # fmt: skip


def test_a_price_times_size_beyond_64_bits_is_refused() -> None:
    big = parse(["4000000000"])
    with pytest.raises(FixedPointError, match="price times size"):
        bars.from_trades(
            "bybit", "AAAUSDT", pa.array([0], pa.int64()), big, big, pa.array([True]), PUBLISHED
        )


def test_identical_bars_agree_and_quiet_minutes_need_no_bar() -> None:
    quiet = {
        "volume": ["3.000", "0"],
        "quote_volume": ["302.5", "0"],
        "taker_buy_volume": ["2.000", "0"],
        "taker_buy_quote_volume": ["201.0", "0"],
    }
    klines = _bars(trades=[7, 0], overrides=quiet)
    trades_only = bars.PitTable(bars.NAME, bars.KEYS, klines.table.slice(0, 1))
    counts = bars.compare(klines, trades_only)
    assert counts["minutes"] == 1  # the quiet minute is not counted
    assert {k: v for k, v in counts.items() if k != "minutes"} == dict.fromkeys(
        ["missing", "extra", *bars.COMPARED, "differing"], 0
    )


def test_differences_are_counted_by_field_and_minute() -> None:
    reference = _bars()
    other = _bars(trades=[7, 3], overrides={"close": ["101.50", "100.4"], "volume": ["3", "0.5"]})
    counts = bars.compare(reference, other)
    # close: 101.5 = 101.50 (same value, other scale), 100.5 vs 100.4 differs; volume is equal
    assert (counts["close"], counts["volume"], counts["trades"], counts["differing"]) == (
        1,
        0,
        1,
        1,
    )
    alone = bars.PitTable(bars.NAME, bars.KEYS, reference.table.slice(0, 1))
    assert bars.compare(reference, alone)["missing"] == 1
    assert bars.compare(alone, reference)["extra"] == 1
