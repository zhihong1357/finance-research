import math
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from finresearch import profile
from finresearch.core.time import to_epoch_us, utc
from finresearch.data import bars
from finresearch.data.fixed import Fixed
from finresearch.data.partitioned import write_partition
from finresearch.data.pit import LookaheadError
from finresearch.data.store import DirectoryRemote, push
from finresearch.data.table import PitTable
from finresearch.registry.models import Boundaries, DateRange, Universe
from tests.test_revised import LIMITS

MINUTE_US = 60_000_000
DAY_US = 1440 * MINUTE_US

U1 = Boundaries(
    boundary_id="B-0001",
    recorded_at=utc(2026, 11, 16),
    universe=Universe.U1,
    exploration=DateRange(start=date(2019, 12, 1), end=date(2024, 1, 31)),
    confirmation=DateRange(start=date(2024, 2, 1), end=date(2025, 10, 31)),
    vault=DateRange(start=date(2025, 11, 1), end=date(2026, 10, 31)),
    decision="0015",
    prev=None,
)


def _bars(symbol: str, minutes: list[tuple[int, int, int]], exchange: str = "binance") -> PitTable:
    """Made-up bars: (minute since 2024-01-01, close in cents, quote volume in cents)."""
    first = to_epoch_us(utc(2024, 1, 1))
    n = len(minutes)
    closes = [c for _, c, _ in minutes]

    def fixed(values: list[int]) -> Fixed:
        return Fixed(pa.array(values, pa.int64()), 2)

    values = {
        "open": fixed(closes),
        "high": fixed(closes),
        "low": fixed(closes),
        "close": fixed(closes),
        "volume": fixed([1] * n),
        "quote_volume": fixed([q for _, _, q in minutes]),
        "taker_buy_volume": fixed([0] * n),
        "taker_buy_quote_volume": fixed([0] * n),
    }
    starts = pa.array([first + m * MINUTE_US for m, _, _ in minutes], pa.int64())
    return bars.bars(
        exchange, symbol, starts, values, pa.array([1] * n, pa.int64()), utc(2024, 2, 2)
    )


def test_days_take_the_last_close_and_the_summed_turnover() -> None:
    # Day 1: closes 1.00 then 1.10, turnover 2.00 + 3.00. Day 2: one minute at 1.21.
    t = _bars("AUSDT", [(0, 100, 200), (5, 110, 300), (1440, 121, 700)]).table
    days = profile.daily(t)
    assert days == {
        "AUSDT": {
            date(2024, 1, 1): profile.Day(1.10, 5.00),
            date(2024, 1, 2): profile.Day(1.21, 7.00),
        }
    }


def test_quantiles_interpolate_between_order_statistics() -> None:
    assert profile.quantile([4, 1, 3, 2], 0.5) == 2.5
    assert profile.quantile([1, 2, 3, 4], 0.1) == pytest.approx(1.3)
    assert profile.quantile([7], 0.99) == 7
    assert math.isnan(profile.quantile([], 0.5))


def test_month_counts_contracts_by_their_median_daily_turnover() -> None:
    days = {
        "A": {
            date(2024, 1, 1): profile.Day(1, 5e6),
            date(2024, 1, 2): profile.Day(1, 2e7),
            date(2024, 1, 3): profile.Day(1, 3e7),
        },
        "B": {date(2024, 1, 1): profile.Day(1, 5e5)},
    }
    m = profile.month_profile(date(2024, 1, 1), days, [1e6, 1e7], [0.5])
    # Medians: A 2e7, B 5e5.
    assert (m.month, m.contracts, m.liquid) == ("2024-01", 2, {"1e+06": 1, "1e+07": 1})
    assert m.turnover_quantiles == {"0.5": pytest.approx((2e7 + 5e5) / 2)}


ORTHOGONAL = {"a": [1.0, -1.0, 0.0, 0.0], "b": [0.0, 0.0, 1.0, -1.0], "c": [1.0, 1.0, -1.0, -1.0]}


def test_uncorrelated_contracts_are_as_many_bets_as_there_are_contracts() -> None:
    b = profile.breadth(date(2024, 1, 31), ORTHOGONAL)
    # C = I: N_eff = 9 / 3; the first eigenvalue is 1, so 2 are left after it.
    assert (b.contracts, b.effective, b.market_share) == (
        3,
        pytest.approx(3.0),
        pytest.approx(1 / 3),
    )
    assert b.effective_ex_market == pytest.approx(2.0)


def test_contracts_that_move_together_are_one_bet() -> None:
    b = profile.breadth(date(2024, 1, 31), {"a": [1.0, 2.0, 3.0], "b": [2.0, 4.0, 6.0]})
    # C is all ones: sum of squares 4, N_eff = 4 / 4; the market explains everything.
    assert (b.effective, b.effective_ex_market, b.market_share) == (
        pytest.approx(1.0),
        0.0,
        pytest.approx(1.0),
    )


def test_the_largest_eigenvalue_of_a_known_matrix() -> None:
    # [[1, r], [r, 1]] has eigenvalues 1 + r and 1 - r.
    assert profile.largest_eigenvalue([[1.0, 0.6], [0.6, 1.0]]) == pytest.approx(1.6)


def test_window_returns_keep_liquid_contracts_with_every_day() -> None:
    end = date(2024, 1, 4)
    full = {date(2024, 1, d): profile.Day(float(d), 2e7) for d in range(1, 5)}
    gap = {d: v for d, v in full.items() if d != date(2024, 1, 2)}
    thin = {d: profile.Day(v.close, 1e3) for d, v in full.items()}
    r = profile.window_returns({"A": full, "B": gap, "C": thin}, end, 3, 1e7)
    # Closes 1, 2, 3, 4: log returns ln 2, ln 1.5, ln (4/3).
    assert list(r) == ["A"]
    assert r["A"] == pytest.approx([math.log(2), math.log(1.5), math.log(4 / 3)])


def test_quarter_ends_inside_a_range() -> None:
    ends = profile.quarter_ends(date(2020, 2, 29), date(2024, 1, 31))
    assert (ends[0], ends[-1], len(ends)) == (date(2020, 3, 31), date(2023, 12, 31), 16)


def test_profile_reads_every_exploration_month_and_nothing_after() -> None:
    asked: list[date] = []

    def read(month: date) -> pa.Table:
        asked.append(month)
        return (
            _bars("AUSDT", [(0, 100, 10**9)]).table
            if month == date(2024, 1, 1)
            else _bars("AUSDT", []).table
        )

    p = profile.profile(
        U1,
        "binance",
        read,
        {
            "liquid_usd": [1e6],
            "quantiles": [0.5],
            "window_days": 90,
            "breadth_min_turnover_usd": 1e7,
        },
    )
    assert (asked[0], asked[-1], len(asked)) == (date(2019, 12, 1), date(2024, 1, 1), 50)
    assert p.months[-1].contracts == 1
    assert len(p.breadth) == 16  # quarters ending 2020-03-31 (the first 90 days in) to 2023-12-31


def test_the_door_reader_cannot_see_past_the_exploration_segment(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    local = tmp_path / "built"
    january = _bars("AUSDT", [(0, 100, 500), (30 * 1440, 101, 500)])  # 2024-01-01 and 2024-01-31
    write_partition(january, local, 1, date(2024, 1, 1), "binance", {}, 1000)
    push(local, remote, "canonical", LIMITS, "build")
    read = profile.door_reader(U1, remote, LIMITS, 1, "binance")
    assert read(date(2024, 1, 1)).num_rows == 2
    with pytest.raises(LookaheadError):
        read(date(2024, 2, 1))
