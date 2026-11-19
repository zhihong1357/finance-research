"""Maintenance margin and liquidation (step 4.3b): hand-worked examples and properties."""

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from finresearch.core.time import utc
from finresearch.sim.costs import Liquidity, Scenario, cost_model
from finresearch.sim.engine import (
    Bar,
    FillRules,
    FillScenario,
    FundingEvent,
    Market,
    Order,
    OrderKind,
    Result,
    fill_rules,
    simulate,
    simulate_all,
)
from finresearch.sim.margin import (
    MarginError,
    maintenance_margin,
    margin_model,
    tiers_from,
    trigger_price,
)

MINUTE = timedelta(minutes=1)
BTC = "BTCUSDT"
MARKETS = {BTC: Market("binance", half_spread=0.0001, daily_volume=1e8, daily_vol=0.04)}
MODEL = margin_model()  # config/simulator.toml: BTCUSDT tier 1 is 0.4% up to 300,000
BTC_TIERS = MODEL.tiers("binance", BTC)


def t(minute: int) -> datetime:
    return utc(2026, 1, 1, 10, minute)


# ---------------------------------------------------------------- the schedule
def test_deductions_make_the_margin_continuous_and_match_binance() -> None:
    # Binance's published maintenance amounts for BTCUSDT: 0, 300, 1,500, 12,000, 132,000
    assert [tier.deduction for tier in BTC_TIERS] == pytest.approx([0, 300, 1500, 12_000, 132_000])
    assert [tier.rate for tier in BTC_TIERS] == [0.004, 0.005, 0.0065, 0.01, 0.02]
    assert BTC_TIERS[-1].cap == math.inf
    # 300,000 sits on the first two tiers' boundary: 300,000 x 0.4% = 1,200 = 300,000 x 0.5% - 300
    assert maintenance_margin(300_000.0, BTC_TIERS) == pytest.approx(1200.0)
    assert maintenance_margin(300_000.01, BTC_TIERS) == pytest.approx(1200.00005)
    # 1,000,000 in tier 3: 1,000,000 x 0.65% - 1,500 = 5,000; a short of the same size is the same
    assert maintenance_margin(-1_000_000.0, BTC_TIERS) == pytest.approx(5000.0)
    assert maintenance_margin(10_000.0, BTC_TIERS) == pytest.approx(40.0)


def test_bad_schedules_are_refused() -> None:
    for bad in (
        [],
        [(100.0, 1.0)],
        [(100.0, 0.02), (50.0, 0.03), (0.0, 0.04)],
        [(1e3, 0.02), (0.0, 0.01)],
    ):
        with pytest.raises(MarginError):
            tiers_from(bad)
    with pytest.raises(MarginError):
        margin_model(
            {
                "margin": {
                    "x": {
                        "liquidation_fee": 0.01,
                        "tiers": {"default": {"caps": [1.0], "rates": [0.1]}},
                    }
                }
            }
        )
    with pytest.raises(MarginError):  # a table without a default
        margin_model(
            {
                "margin": {
                    "x": {"liquidation_fee": 0.01, "tiers": {"A": {"caps": [], "rates": [0.1]}}}
                }
            }
        )


def test_trigger_price_by_hand() -> None:
    # long 100 with -9,010 cash: 100 p - 9,010 = 0.004 x 100 p  ->  p = 9,010 / 99.6 = 90.461847
    assert trigger_price(100.0, -9010.0, 0.0, BTC_TIERS) == pytest.approx(90.461847, abs=1e-6)
    # short 100 with 10,990 cash: 10,990 - 100 p = 0.004 x 100 p  ->  p = 10,990 / 100.4
    #   = 109.462151
    assert trigger_price(-100.0, 10_990.0, 0.0, BTC_TIERS) == pytest.approx(109.462151, abs=1e-6)
    # long 5,000 with -480,000 cash: in tier 1 p would be 480,000 / 4,980 = 96.39 (notional 481,928,
    # past tier 1); in tier 2: 5,000 p - 480,000 = 0.005 x 5,000 p - 300  ->  p = 479,700 / 4,975
    p = trigger_price(5000.0, -480_000.0, 0.0, BTC_TIERS)
    assert p == pytest.approx(479_700 / 4975)
    assert p == pytest.approx(96.422111, abs=1e-6)
    # another position's margin of 100 counts against this one: p = (100 + 9,010) / 99.6 = 91.465863
    assert trigger_price(100.0, -9010.0, 100.0, BTC_TIERS) == pytest.approx(91.465863, abs=1e-6)
    # a long the cash fully pays for is never liquidated; a short already under water always is
    assert trigger_price(100.0, 0.0, 0.0, BTC_TIERS) == 0.0
    assert trigger_price(-100.0, -1.0, 0.0, BTC_TIERS) == 0.0
    with pytest.raises(MarginError):
        trigger_price(0.0, 1.0, 0.0, BTC_TIERS)


@settings(max_examples=300)
@given(
    st.floats(min_value=0.01, max_value=1e5) | st.floats(min_value=-1e5, max_value=-0.01),
    st.floats(min_value=-1e9, max_value=1e9),
    st.floats(min_value=0.0, max_value=1e6),
)
def test_at_the_trigger_price_equity_equals_the_margin(
    position: float, rest: float, mm_rest: float
) -> None:
    p = trigger_price(position, rest, mm_rest, BTC_TIERS)
    if p > 0.0:
        equity = rest + position * p
        required = mm_rest + maintenance_margin(position * p, BTC_TIERS)
        assert math.isclose(
            equity, required, rel_tol=1e-9, abs_tol=1e-6 * (abs(rest) + mm_rest + 1)
        )


# ---------------------------------------------------------------- liquidation in the simulator
def _bars(*rows: tuple[float, float, float, float]) -> list[Bar]:
    return [Bar(BTC, t(m), o, h, lo, c) for m, (o, h, lo, c) in enumerate(rows)]


FLAT = (100.0, 100.0, 100.0, 100.0)
CALM = (100.0, 100.5, 99.0, 100.0)  # its low 99 stays above the trigger 90.46
BUY_10X = Order(t(0), BTC, 100.0)  # market, fills at the 10:01 open: 10,000 on 1,000 of cash


def _run(
    bars: list[Bar],
    orders: list[Order],
    funding: list[FundingEvent] | None = None,
    rules: FillRules | None = None,
    model_config: Mapping[str, Any] | None = None,
) -> Result:
    return simulate(
        bars,
        orders,
        funding or [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        rules or fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
        initial_cash=1000.0,
        margin=margin_model(model_config),
    )


def _close_costs(notional: float, impact_multiplier: float = 1.0) -> float:
    """Taker costs of a BTCUSDT trade: fee 0.05%, half spread 1 bp, impact 0.04 sqrt(n / 1e8)."""
    return notional * (0.0005 + 0.0001) + impact_multiplier * notional * 0.04 * math.sqrt(
        notional / 1e8
    )


def test_a_10x_long_is_liquidated_at_its_trigger_price() -> None:
    # Buy 100 at 100: costs fee 5 + spread 1 + impact 10,000 x 0.04 x 0.01 = 4, cash -9,010.
    # Trigger 9,010 / 99.6 = 90.461847. The 10:02 bar opens at 99 and falls to 89, through it:
    # closed at 90.461847, notional 9,046.18; costs 4.523092 + 0.904618 + 3.441582 = 8.869293;
    # left -9,010 + 9,046.184739 - 8.869293 = 27.315446; the 1.5% fee (135.69) takes all of it.
    r = _run(_bars(FLAT, CALM, (99.0, 99.0, 89.0, 92.0), FLAT), [BUY_10X])
    liq = r.liquidation
    assert liq is not None
    assert (liq.at, liq.instrument, liq.closed) == (t(2), BTC, {BTC: 100.0})
    assert liq.trigger == pytest.approx(90.461847, abs=1e-6)
    assert liq.price == liq.trigger
    assert liq.costs == pytest.approx(8.869293, abs=1e-6)
    assert _close_costs(100 * liq.price) == pytest.approx(liq.costs)
    assert liq.fee == pytest.approx(27.315446, abs=1e-6)
    assert liq.cash == 0.0
    assert r.cash == 0.0
    assert not liq.bankrupt
    assert r.positions == {BTC: 0.0}
    # equity: 1,000 before the fill, 990 after the calm bar, then 0 to the end
    assert [e for _, e in r.equity] == [1000.0, pytest.approx(990.0), 0.0, 0.0]


def test_the_fee_is_charged_in_full_when_the_account_can_pay_it() -> None:
    # the same with a 0.1% fee: 9.046185, out of the 27.315446 left: 18.269261 remains
    cfg = {
        "margin": {
            "binance": {
                "liquidation_fee": 0.001,
                "tiers": {"default": {"caps": [300_000.0], "rates": [0.004, 0.005]}},
            }
        }
    }
    r = _run(_bars(FLAT, CALM, (99.0, 99.0, 89.0, 92.0)), [BUY_10X], model_config=cfg)
    assert r.liquidation is not None
    assert r.liquidation.fee == pytest.approx(9.046185, abs=1e-6)
    assert r.cash == pytest.approx(18.269261, abs=1e-6)


def test_a_gap_through_zero_is_booked_negative() -> None:
    # The 10:02 bar opens at 85, already below the trigger 90.46: closed at the open.
    # -9,010 + 8,500 - costs (4.25 + 0.85 + 8,500 x 0.04 x sqrt(8.5e-5) = 3.134645) = -518.234645.
    # Nothing is left for the fee. An exchange's insurance fund would usually absorb the -518;
    # the simulator keeps it.
    r = _run(_bars(FLAT, CALM, (85.0, 86.0, 84.0, 85.0), FLAT), [BUY_10X])
    liq = r.liquidation
    assert liq is not None
    assert liq.price == 85.0
    assert liq.fee == 0.0
    assert liq.cash == pytest.approx(-518.234645, abs=1e-6)
    assert liq.bankrupt
    assert r.equity[-1][1] == pytest.approx(-518.234645, abs=1e-6)


def test_stress_liquidates_at_the_bars_extreme() -> None:
    # the first example with liquidate_at_extreme and impact doubled. The buy pays impact 8, not 4:
    # cash -9,014, trigger 9,014 / 99.6 = 90.502008, crossed by the low 89. Closed at the low:
    # -9,014 + 8,900 - (5.34 + 2 x 8,900 x 0.04 x sqrt(8.9e-5) = 6.716995) = -126.056995
    rules = FillRules(timedelta(seconds=60), 1.0, 2.0, 5, liquidate_at_extreme=True)
    r = _run(_bars(FLAT, CALM, (99.0, 99.0, 89.0, 92.0)), [BUY_10X], rules=rules)
    liq = r.liquidation
    assert liq is not None
    assert liq.price == 89.0
    assert liq.trigger == pytest.approx(90.502008, abs=1e-6)
    assert liq.cash == pytest.approx(-9014.0 + 8900.0 - _close_costs(8900.0, 2.0))
    assert liq.cash == pytest.approx(-126.056995, abs=1e-6)


def test_a_short_is_liquidated_above_its_trigger() -> None:
    # sell 100 at 100: cash 1,000 + 10,000 - 10 = 10,990; trigger 10,990 / 100.4 = 109.462151.
    # The 10:02 bar opens at 101 and reaches 112: bought back at 109.462151, notional 10,946.215139;
    # costs 5.473108 + 1.094622 + 10,946.215139 x 0.04 x sqrt(1.0946e-4) = 4.580954,
    # total 11.148683;
    # left 10,990 - 10,946.215139 - 11.148683 = 32.636177, all taken by the fee
    sell = Order(t(0), BTC, -100.0)
    r = _run(_bars(FLAT, CALM, (101.0, 112.0, 100.0, 110.0)), [sell])
    liq = r.liquidation
    assert liq is not None
    assert liq.price == pytest.approx(109.462151, abs=1e-6)
    left = 10_990.0 - 100 * liq.price - _close_costs(100 * liq.price)
    assert left == pytest.approx(32.636177, abs=1e-6)
    assert (liq.fee, r.cash) == (pytest.approx(left), 0.0)


def test_a_liquidation_ends_the_runs_trading() -> None:
    later = Order(t(2), BTC, 1.0)  # active at 10:03, after the liquidation
    r = _run(_bars(FLAT, CALM, (99.0, 99.0, 89.0, 92.0), FLAT, FLAT), [BUY_10X, later])
    assert [f.order for f in r.fills] == [0]
    assert r.unfilled == [1]


def test_resting_fills_in_the_liquidations_bar_are_undone() -> None:
    # A sell limit at 100.5 would close the long when the 10:02 bar reaches 101; but that bar also
    # falls to 89, through the trigger, and cannot say which came first: liquidated, sale undone.
    close = Order(t(0), BTC, -100.0, OrderKind.LIMIT, limit_price=100.5)
    r = _run(_bars(FLAT, CALM, (99.0, 101.0, 89.0, 92.0)), [BUY_10X, close])
    assert [f.order for f in r.fills] == [0]
    assert r.unfilled == [1]
    assert r.liquidation is not None
    assert r.liquidation.price == pytest.approx(90.461847, abs=1e-6)
    assert r.cash == 0.0
    # without the fall the sale goes through and nothing is liquidated
    r = _run(_bars(FLAT, CALM, (99.0, 101.0, 92.0, 95.0)), [BUY_10X, close])
    assert r.liquidation is None
    assert [(f.order, f.liquidity) for f in r.fills] == [(0, Liquidity.TAKER), (1, Liquidity.MAKER)]


def test_a_resting_fill_that_opens_a_leveraged_position_is_checked_in_its_bar() -> None:
    # Buy limit 100 at 99.5, filled in the 10:01 bar (low 88): cash 1,000 - 9,950 - 1.99
    # = -8,951.99;
    # trigger 8,951.99 / 99.6 = 89.879418, and the same bar's low 88 is below it: liquidated in
    # that bar at min(open 100, 89.879418)
    buy = Order(t(0), BTC, 100.0, OrderKind.LIMIT, limit_price=99.5)
    r = _run(_bars(FLAT, (100.0, 100.0, 88.0, 95.0), FLAT), [buy])
    liq = r.liquidation
    assert liq is not None
    assert liq.at == t(1)
    assert liq.price == pytest.approx(89.879418, abs=1e-6)


def test_funding_inside_the_liquidations_bar_takes_the_worse_position() -> None:
    # Settlement at 10:02:30, inside the liquidation's bar, mark 90. Paying funding (+0.1%): booked
    # on the long 100 as if not yet liquidated, 100 x 90 x 0.1% = 9. Receiving (-0.1%): booked as
    # flat.
    bars = _bars(FLAT, CALM, (99.0, 99.0, 89.0, 92.0), FLAT)
    at = t(2) + timedelta(seconds=30)
    paying = _run(bars, [BUY_10X], [FundingEvent(BTC, at, 0.001, 90.0)])
    (paid,) = paying.funding
    assert (paid.position, paid.cash) == (100.0, pytest.approx(-9.0))
    assert paying.cash == pytest.approx(-9.0)
    receiving = _run(bars, [BUY_10X], [FundingEvent(BTC, at, -0.001, 90.0)])
    assert receiving.funding == []
    assert receiving.cash == 0.0


def test_simulate_all_checks_margin_by_default() -> None:
    bars = _bars(FLAT, CALM, (99.0, 99.0, 89.0, 92.0), FLAT)
    results = simulate_all(bars, [BUY_10X], [], MARKETS, MINUTE, initial_cash=1000.0)
    assert all(r.liquidation is not None for r in results.values())
    ledgers = simulate_all(
        bars, [BUY_10X], [], MARKETS, MINUTE, initial_cash=1000.0, with_margin=False
    )
    assert all(r.liquidation is None for r in ledgers.values())


# ---------------------------------------------------------------- regression: far from liquidation
T0 = datetime(2026, 1, 1, tzinfo=UTC)
PROPERTY_MARKETS = {
    "A": Market("binance", 0.0001, 50_000_000.0, 0.04),
    "B": Market("bybit", 0.0002, 5_000_000.0, 0.06),
}


def _fields(r: Result) -> tuple[object, ...]:
    return (r.fills, r.funding, r.unfilled, r.positions, r.cash, r.equity)


def test_an_unlevered_run_is_unchanged_by_the_margin_check() -> None:
    # test_engine's hand scenario with cash behind every trade: bit-for-bit what it gives without
    from tests.sim.test_engine import BARS, FUNDING, ORDERS
    from tests.sim.test_engine import MARKETS as ENGINE_MARKETS

    markets = {"BTC": ENGINE_MARKETS["BTC"]}
    for fill in FillScenario:
        args = (
            BARS,
            ORDERS,
            FUNDING,
            markets,
            cost_model(Scenario.NORMAL),
            fill_rules(fill),
            MINUTE,
            1000.0,
        )
        with_margin, without = simulate(*args, margin=MODEL), simulate(*args)
        assert with_margin.liquidation is None
        assert _fields(with_margin) == _fields(without)


@settings(max_examples=200)
@given(st.data(), st.sampled_from(list(FillScenario)))
def test_far_from_liquidation_the_result_is_unchanged(
    data: st.DataObject, fill: FillScenario
) -> None:
    from tests.sim.test_engine_properties import worlds

    path, orders, funding = data.draw(worlds())
    # every order is at most 50 units at under 1,600: a million in cash is never at risk
    args = (
        path,
        orders,
        funding,
        PROPERTY_MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(fill),
        MINUTE,
        1e6,
    )
    with_margin, without = simulate(*args, margin=MODEL), simulate(*args)
    assert with_margin.liquidation is None
    assert _fields(with_margin) == _fields(without)


@settings(max_examples=200)
@given(st.data(), st.sampled_from(list(FillScenario)), st.floats(min_value=0.0, max_value=2000.0))
def test_after_a_liquidation_nothing_trades_and_the_books_balance(
    data: st.DataObject, fill: FillScenario, cash: float
) -> None:
    from tests.sim.test_engine_properties import worlds

    path, orders, funding = data.draw(worlds())
    r = simulate(
        path,
        orders,
        funding,
        PROPERTY_MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(fill),
        MINUTE,
        cash,
        MODEL,
    )
    assert sorted([f.order for f in r.fills] + r.unfilled) == list(range(len(orders)))
    liq = r.liquidation
    if liq is None:
        return
    assert all(f.at <= liq.at for f in r.fills)
    assert all(q == 0.0 for q in r.positions.values())
    assert liq.fee >= 0.0
    # cash: start, minus every fill, plus funding, plus the closing trades, minus their costs and
    # the fee
    closes = _closes_before(path, liq.instrument, liq.at)
    closing = math.fsum(
        q * (liq.price if n == liq.instrument else closes[n]) for n, q in liq.closed.items()
    )
    spent = math.fsum(f.quantity * f.price + f.cost.total for f in r.fills)
    received = math.fsum(p.cash for p in r.funding)
    expected = cash - spent + received + closing - liq.costs - liq.fee
    assert math.isclose(r.cash, expected, rel_tol=1e-9, abs_tol=1e-6)
    assert math.isclose(r.equity[-1][1], r.cash, rel_tol=1e-12, abs_tol=1e-9)
    if liq.fee > 0.0:  # the fee never takes the account below zero by itself
        assert liq.cash >= -1e-9


def _closes_before(path: list[Bar], instrument: str, at: datetime) -> dict[str, float]:
    """Each instrument's latest close when the bar of ``instrument`` at ``at`` is traded."""
    here = next(k for k, b in enumerate(path) if b.instrument == instrument and b.start == at)
    return {b.instrument: b.close for b in path[:here]}
