import math
from datetime import datetime, timedelta

import pytest

from finresearch.core.time import utc
from finresearch.sim.costs import Liquidity, Scenario, cost_model
from finresearch.sim.engine import (
    Bar,
    FillScenario,
    FundingEvent,
    Market,
    Order,
    OrderKind,
    Result,
    SimulationError,
    fill_rules,
    simulate,
    simulate_all,
)

MINUTE = timedelta(minutes=1)
BTC = Market(exchange="binance", half_spread=0.0001, daily_volume=100_000_000.0, daily_vol=0.04)
MARKETS = {"BTC": BTC}


def t(minute: int) -> datetime:
    return utc(2026, 1, 1, 10, minute)


# (minute, open, high, low, close)
PATH = [
    (0, 100.0, 101.0, 99.0, 100.0),
    (1, 101.0, 102.0, 100.0, 101.5),
    (2, 101.5, 103.0, 101.0, 102.5),
    (3, 102.5, 102.8, 101.5, 102.0),
    (4, 102.0, 102.2, 100.5, 101.0),
    (5, 101.0, 101.5, 100.0, 100.8),
]
BARS = [Bar("BTC", utc(2026, 1, 1, 10, m), o, h, lo, c) for m, o, h, lo, c in PATH]
ORDERS = [
    # 0: buy 2 at market, decided at 10:00
    Order(utc(2026, 1, 1, 10, 0), "BTC", 2.0),
    # 1: sell 2 with a limit at 102.9, decided at 10:01, good until 10:04
    Order(
        utc(2026, 1, 1, 10, 1),
        "BTC",
        -2.0,
        OrderKind.LIMIT,
        limit_price=102.9,
        expires_at=utc(2026, 1, 1, 10, 4),
    ),
]
FUNDING = [FundingEvent("BTC", utc(2026, 1, 1, 10, 3), rate=0.0001, mark_price=102.0)]


def run(scenario: FillScenario, costs: Scenario = Scenario.NORMAL) -> Result:
    return simulate(BARS, ORDERS, FUNDING, MARKETS, cost_model(costs), fill_rules(scenario), MINUTE)


def test_conservative_scenario_by_hand() -> None:
    r = run(FillScenario.CONSERVATIVE)
    buy, sell = r.fills
    # Buy: latency 60 s, active from 10:01, so the 10:01 bar's open 101. Notional 202.
    # fee 202 x 0.05% = 0.101; spread 202 x 1 bp = 0.0202;
    # impact 202 x 1.0 x 0.04 x sqrt(202 / 1e8) = 202 x 0.04 x 0.00142127 = 0.0114838
    assert (buy.at, buy.price, buy.quantity, buy.liquidity) == (t(1), 101.0, 2.0, Liquidity.TAKER)
    assert buy.cost.fee == pytest.approx(0.101)
    assert buy.cost.spread == pytest.approx(0.0202)
    assert buy.cost.impact == pytest.approx(202 * 0.04 * math.sqrt(202 / 1e8))
    assert buy.cost.impact == pytest.approx(0.0114838, abs=1e-7)
    # Sell limit 102.9: active from 10:02; that bar's high 103 trades through it. Fills at the
    # limit as maker: fee 205.8 x 0.02% = 0.04116, no spread, no impact.
    assert (sell.at, sell.price, sell.quantity, sell.liquidity) == (
        t(2),
        102.9,
        -2.0,
        Liquidity.MAKER,
    )
    assert (sell.cost.fee, sell.cost.spread, sell.cost.impact) == (pytest.approx(0.04116), 0.0, 0.0)
    # Flat before the 10:03 funding: nothing paid.
    assert r.funding == []
    # Cash: -202 - 0.1326838 + 205.8 - 0.04116 = 3.6261562; flat, so equity is the cash.
    assert r.positions == {"BTC": 0.0}
    assert r.cash == pytest.approx(3.6261562, abs=1e-7)
    assert r.equity[-1] == (t(6), pytest.approx(3.6261562, abs=1e-7))
    # After the 10:01 bar: cash -202.1326838, 2 x close 101.5 = 203 -> equity 0.8673162
    assert r.equity[1] == (t(2), pytest.approx(0.8673162, abs=1e-7))
    assert r.unfilled == []


def test_stress_scenario_by_hand() -> None:
    r = run(FillScenario.STRESS)
    (buy,) = r.fills
    # Latency doubled: the buy is active from 10:02 and fills at 101.5. Notional 203.
    # fee 0.1015, spread 0.0203, impact doubled: 2 x 203 x 0.04 x sqrt(203 / 1e8) = 0.0231385
    assert (buy.at, buy.price) == (t(2), 101.5)
    assert buy.cost.impact == pytest.approx(2 * 203 * 0.04 * math.sqrt(203 / 1e8))
    assert buy.cost.impact == pytest.approx(0.0231385, abs=1e-7)
    # The limit sell is active from 10:03: that bar's high 102.8 does not trade through 102.9,
    # and the 10:04 bar ends after its 10:04 expiry, so it cannot count.
    assert r.unfilled == [1]
    # Long 2 at the 10:03 funding: pays 2 x 102 x 0.01% = 0.0204
    (paid,) = r.funding
    assert (paid.position, paid.cash) == (2.0, pytest.approx(-0.0204))
    # Cash: -203 - 0.1449385 - 0.0204 = -203.1653385; 2 x last close 100.8 = 201.6
    assert r.cash == pytest.approx(-203.1653385, abs=1e-7)
    assert r.equity[-1][1] == pytest.approx(-1.5653385, abs=1e-7)


def test_one_call_gives_every_scenario() -> None:
    # no cash behind the trades: a profit-and-loss ledger, no margin account (test_margin.py)
    results = simulate_all(BARS, ORDERS, FUNDING, MARKETS, MINUTE, with_margin=False)
    assert set(results) == {(c, f) for c in Scenario for f in FillScenario}
    equity = {k: r.equity[-1][1] for k, r in results.items()}
    # Conservative fills: without costs the round trip makes 205.8 - 202 = 3.8;
    # normal costs 0.1738438, doubled 0.3476876
    assert equity[(Scenario.ZERO, FillScenario.CONSERVATIVE)] == pytest.approx(3.8)
    assert equity[(Scenario.NORMAL, FillScenario.CONSERVATIVE)] == pytest.approx(
        3.6261562, abs=1e-7
    )
    assert equity[(Scenario.DOUBLE, FillScenario.CONSERVATIVE)] == pytest.approx(
        3.4523124, abs=1e-7
    )
    # Here stress does worse under the same costs; in general it need not (the next test)
    for c in Scenario:
        assert equity[(c, FillScenario.STRESS)] <= equity[(c, FillScenario.CONSERVATIVE)]


def test_stress_is_not_a_bound_on_the_result() -> None:
    # A buy limit at 99 into a falling market. Conservative: active 10:01, that bar's low 98 trades
    # through, buys 2 at 99; stress: active 10:02, buys only half, 1 at 99. Both end at 90.
    # Conservative: -198 - 198 x 0.02% + 2 x 90 = -18.0396; stress: -99 - 0.0198 + 90 = -9.0198
    # (the 10:02 bar opens at 99.5, above the limit: had it opened below, the stress order would
    # take at that open instead, the next tests)
    bars = [
        Bar("BTC", t(0), 100.0, 100.0, 100.0, 100.0),
        Bar("BTC", t(1), 100.0, 100.0, 98.0, 98.0),
        Bar("BTC", t(2), 99.5, 99.5, 89.0, 90.0),
    ]
    order = Order(t(0), "BTC", 2.0, OrderKind.LIMIT, limit_price=99.0)
    results = simulate_all(bars, [order], [], MARKETS, MINUTE, with_margin=False)
    conservative = results[(Scenario.NORMAL, FillScenario.CONSERVATIVE)].equity[-1][1]
    stress = results[(Scenario.NORMAL, FillScenario.STRESS)].equity[-1][1]
    assert conservative == pytest.approx(-18.0396)
    assert stress == pytest.approx(-9.0198)
    assert stress > conservative


def _conservative(orders: list[Order], funding: list[FundingEvent] | None = None) -> Result:
    return simulate(
        BARS,
        orders,
        funding or [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )


def test_impact_is_charged_on_the_metaorder() -> None:
    # c(x) = 1.0 x 0.04 x x^1.5 / sqrt(1e8). Buys of 1 decided at 10:00 and 10:01 fill at the
    # 10:01 open 101 and the 10:02 open 101.5: one metaorder. The first pays c(101) = 0.0040601,
    # the second c(202.5) - c(101) = 0.0115265 - 0.0040601 = 0.0074664, not c(101.5) = 0.0040903.
    r = _conservative([Order(t(0), "BTC", 1.0), Order(t(1), "BTC", 1.0)])
    first, second = (f.cost.impact for f in r.fills)
    assert first == pytest.approx(0.0040601, abs=1e-7)
    assert second == pytest.approx(0.0074664, abs=1e-7)
    # a sell in between starts a new metaorder: the second buy pays c(101.5) on its own
    r = _conservative([Order(t(0), "BTC", 1.0), Order(t(0), "BTC", -0.5), Order(t(1), "BTC", 1.0)])
    assert r.fills[2].cost.impact == pytest.approx(0.0040903, abs=1e-7)
    # two buys of 1 in the same bar pay what one buy of 2 pays: c(202) = 0.0114838,
    # where pricing each alone would give 2 c(101) = 0.0081203
    r = _conservative([Order(t(0), "BTC", 1.0), Order(t(0), "BTC", 1.0)])
    assert r.costs == pytest.approx(_conservative([Order(t(0), "BTC", 2.0)]).costs)
    assert math.fsum(f.cost.impact for f in r.fills) == pytest.approx(0.0114838, abs=1e-7)


def test_a_metaorder_ends_with_its_window() -> None:
    # a window of two minutes: the 10:01 and 10:02 buys are one metaorder, the 10:03 buy a new one
    cfg = {
        "fees": {"binance": {"maker": 0.0002, "taker": 0.0005}},
        "impact": {"y": 1.0, "metaorder_hours": 2 / 60},
        "scenarios": {"normal": 1.0},
    }
    orders = [Order(t(m), "BTC", 1.0) for m in range(3)]
    r = simulate(
        BARS,
        orders,
        [],
        MARKETS,
        cost_model(Scenario.NORMAL, cfg),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )
    alone = 0.04 * (1.0 * 102.5) ** 1.5 / 1e4  # the 10:03 open, 102.5: 0.0041510
    assert r.fills[2].cost.impact == pytest.approx(alone)
    assert alone == pytest.approx(0.0041510, abs=1e-7)


def test_a_limit_order_needs_the_whole_bar_before_its_expiry() -> None:
    # buy limit 100.2, active from 10:01. Good until 10:01:30: the 10:01 bar (low 100.0, through
    # the limit) ends at 10:02, after the expiry, so it does not fill. Good until 10:02: it does.
    straddles = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, 100.2, t(1) + timedelta(seconds=30))
    assert _conservative([straddles]).unfilled == [0]
    ends_in_time = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, 100.2, t(2))
    assert [f.at for f in _conservative([ends_in_time]).fills] == [t(1)]
    # a market order fills at the open, so it only needs the bar to start by its expiry
    market = Order(t(0), "BTC", 1.0, OrderKind.MARKET, None, t(1))
    assert [f.at for f in _conservative([market]).fills] == [t(1)]


def test_funding_at_the_last_bar_end_reaches_the_equity_curve() -> None:
    # long 2 from 10:01; funding at 10:06, when the last bar ends: pays 2 x 100 x 0.1% = 0.2
    funding = [FundingEvent("BTC", t(6), rate=0.001, mark_price=100.0)]
    r = _conservative([Order(t(0), "BTC", 2.0)], funding)
    (paid,) = r.funding
    assert paid.cash == pytest.approx(-0.2)
    assert r.equity[-1] == (t(6), pytest.approx(r.cash + 2 * 100.8))
    # funding between bar ends shows in the next point: at 10:03:30 it is in the 10:04 point
    mid = [FundingEvent("BTC", t(3) + timedelta(seconds=30), rate=0.001, mark_price=100.0)]
    with_mid, without = (
        _conservative([Order(t(0), "BTC", 2.0)], mid),
        _conservative([Order(t(0), "BTC", 2.0)]),
    )
    gap = [a[1] - b[1] for a, b in zip(with_mid.equity, without.equity, strict=True)]
    # points at 10:01 to 10:06: the 10:04 point is the first to include it
    assert gap == [0.0, 0.0, 0.0, pytest.approx(-0.2), pytest.approx(-0.2), pytest.approx(-0.2)]


def test_touching_a_limit_is_not_trading_through_it() -> None:
    # buy limit at 100.0, exactly the 10:01 bar's low: no fill; 10:05's low 100.0 also only touches
    touch = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, limit_price=100.0)
    r = simulate(
        BARS,
        [touch],
        [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )
    assert r.fills == []
    assert r.unfilled == [0]
    # at 100.2 the 10:01 bar's low of 100.0 trades through it
    through = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, limit_price=100.2)
    r = simulate(
        BARS,
        [through],
        [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )
    assert [(f.at, f.price) for f in r.fills] == [(t(1), 100.2)]


def test_a_gap_does_not_improve_a_resting_limit_fill() -> None:
    # buy limit at 101.2 active from 10:01. The 10:01 bar opens at 101.5, above it, and does not
    # trade through (low 101.4): the order rests. The 10:02 bar gaps down to open at 100.5: a
    # resting order fills at its limit, 101.2, as a maker, not at the better open
    bars = [
        Bar("BTC", t(0), 101.5, 101.5, 101.5, 101.5),
        Bar("BTC", t(1), 101.5, 101.8, 101.4, 101.6),
        Bar("BTC", t(2), 100.5, 100.8, 100.2, 100.6),
    ]
    order = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, limit_price=101.2)
    r = _conservative_on(bars, [order])
    assert [(f.at, f.price, f.liquidity) for f in r.fills] == [(t(2), 101.2, Liquidity.MAKER)]
    assert r.fills[0].cost.fee == pytest.approx(101.2 * 0.0002)


def _conservative_on(bars: list[Bar], orders: list[Order]) -> Result:
    return simulate(
        bars,
        orders,
        [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )


# ---------------------------------------------------------------- a limit order that takes at once
def test_a_limit_order_through_at_its_first_open_is_a_taker() -> None:
    # buy limit at 101.2, active from 10:01; the 10:01 bar opens at 101, already below the limit.
    # It would not rest but take the ask at once: all of it at the open 101, as a taker.
    # Notional 202: fee 202 x 0.05% = 0.101; spread 202 x 1 bp = 0.0202;
    # impact 202 x 1.0 x 0.04 x sqrt(202 / 1e8) = 0.0114838: what a market buy of 2 pays
    order = Order(t(0), "BTC", 2.0, OrderKind.LIMIT, limit_price=101.2)
    r = _conservative([order])
    (fill,) = r.fills
    assert (fill.at, fill.price, fill.quantity, fill.liquidity) == (
        t(1),
        101.0,
        2.0,
        Liquidity.TAKER,
    )
    assert fill.cost.fee == pytest.approx(0.101)
    assert fill.cost.spread == pytest.approx(0.0202)
    assert fill.cost.impact == pytest.approx(0.0114838, abs=1e-7)
    # the same as a market buy of 2 decided at 10:00
    market = _conservative([Order(t(0), "BTC", 2.0)])
    assert (r.cash, r.equity) == (market.cash, market.equity)


def test_a_sell_limit_through_at_its_first_open_is_a_taker() -> None:
    # sell limit at 101.0, active from 10:02; the 10:02 bar opens at 101.5, above it: sells 1 at
    # 101.5 as a taker. fee 101.5 x 0.05% = 0.05075, spread 0.01015,
    # impact 101.5 x 0.04 x sqrt(101.5 / 1e8) = 0.0040903
    order = Order(t(1), "BTC", -1.0, OrderKind.LIMIT, limit_price=101.0)
    (fill,) = _conservative([order]).fills
    assert (fill.at, fill.price, fill.liquidity) == (t(2), 101.5, Liquidity.TAKER)
    assert fill.cost.fee == pytest.approx(0.05075)
    assert fill.cost.spread == pytest.approx(0.01015)
    assert fill.cost.impact == pytest.approx(0.0040903, abs=1e-7)


def test_a_taking_limit_order_fills_in_full_under_stress() -> None:
    # stress: active from 10:02 (latency 120 s), that bar opens at 101.5, below a buy limit of
    # 102: it takes at once, all 2 (the half fill is for resting orders), impact doubled:
    # fee 203 x 0.05% = 0.1015, spread 0.0203, impact 2 x 203 x 0.04 x sqrt(203 / 1e8) = 0.0231385
    order = Order(t(0), "BTC", 2.0, OrderKind.LIMIT, limit_price=102.0)
    r = simulate(
        BARS,
        [order],
        [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.STRESS),
        MINUTE,
    )
    (fill,) = r.fills
    assert (fill.at, fill.price, fill.quantity, fill.liquidity) == (
        t(2),
        101.5,
        2.0,
        Liquidity.TAKER,
    )
    assert fill.cost.total == pytest.approx(0.1015 + 0.0203 + 0.0231385, abs=1e-7)


def test_touching_the_limit_at_the_open_is_not_taking() -> None:
    # buy limit at 101.0, exactly the 10:01 open: not through, so it rests; the 10:01 low of 100.0
    # trades through it and it fills at 101.0 as a maker (fee 101 x 0.02% = 0.0202)
    order = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, limit_price=101.0)
    (fill,) = _conservative([order]).fills
    assert (fill.at, fill.price, fill.liquidity) == (t(1), 101.0, Liquidity.MAKER)
    assert (fill.cost.fee, fill.cost.spread, fill.cost.impact) == (pytest.approx(0.0202), 0.0, 0.0)


def test_a_taking_limit_order_needs_only_its_first_open_before_the_expiry() -> None:
    # buy limit 101.2 good until 10:01:30: the 10:01 bar straddles the expiry, which would stop a
    # resting fill, but this one takes at the 10:01 open, before the expiry
    order = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, 101.2, t(1) + timedelta(seconds=30))
    assert [(f.at, f.liquidity) for f in _conservative([order]).fills] == [(t(1), Liquidity.TAKER)]


def test_a_short_receives_positive_funding() -> None:
    short = Order(t(0), "BTC", -1.0)
    r = simulate(
        BARS,
        [short],
        FUNDING,
        MARKETS,
        cost_model(Scenario.ZERO),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )
    (paid,) = r.funding
    assert (paid.position, paid.cash) == (-1.0, pytest.approx(0.0102))  # 1 x 102 x 0.01%


def test_an_order_after_the_last_bar_is_left_unfilled() -> None:
    late = Order(t(5), "BTC", 1.0)
    r = simulate(
        BARS,
        [late],
        [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )
    assert (r.fills, r.unfilled) == ([], [0])


def test_two_instruments_give_one_equity_point_per_instant() -> None:
    eth = [Bar("ETH", b.start, 10.0, 10.0, 10.0, 10.0) for b in BARS]
    markets = MARKETS | {"ETH": Market("bybit", 0.0001, 1e7, 0.05)}
    bars = sorted(BARS + eth, key=lambda b: (b.start, b.instrument))
    r = simulate(
        bars,
        [],
        [],
        markets,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )
    assert [ts for ts, _ in r.equity] == [b.start + MINUTE for b in BARS]


def test_invalid_inputs_are_refused() -> None:
    normal, rules = cost_model(Scenario.NORMAL), fill_rules(FillScenario.CONSERVATIVE)
    bad_runs = [
        ([BARS[1], BARS[0]], []),  # bars out of order
        ([Bar("BTC", t(0), 100.0, 99.0, 98.0, 100.0)], []),  # high below open
        (BARS, [Order(t(0), "BTC", 0.0)]),
        (BARS, [Order(t(0), "SOL", 1.0)]),
        (BARS, [Order(t(0), "BTC", 1.0, OrderKind.LIMIT)]),
        (BARS, [Order(t(0), "BTC", 1.0, limit_price=100.0)]),
        ([*BARS, Bar("ETH", t(0) + timedelta(seconds=30), 10.0, 10.0, 10.0, 10.0)], []),  # off grid
        ([Bar("BTC", t(0), 100.0, math.inf, 99.0, 100.0)], []),
        ([Bar("BTC", t(0), math.nan, 101.0, 99.0, 100.0)], []),
        (BARS, [Order(t(0), "BTC", 1.0, OrderKind.LIMIT, -1.0)]),
        (BARS, [Order(t(0), "BTC", 1.0, OrderKind.LIMIT, math.nan)]),
        (BARS, [Order(t(1), "BTC", 1.0, OrderKind.MARKET, None, t(0))]),  # expires before decided
    ]
    for bars, orders in bad_runs:
        with pytest.raises(SimulationError):
            simulate(bars, orders, [], MARKETS, normal, rules, MINUTE)
    bad_funding = [
        FundingEvent("BTC", t(6) + timedelta(seconds=1), 0.0001, 100.0),  # after the last bar
        FundingEvent("BTC", t(3), math.nan, 100.0),
        FundingEvent("BTC", t(3), 0.0001, math.nan),
        FundingEvent("BTC", t(3), 0.0001, 0.0),
    ]
    for event in bad_funding:
        with pytest.raises(SimulationError):
            simulate(BARS, [], [event], MARKETS, normal, rules, MINUTE)
    with pytest.raises(SimulationError):
        simulate(BARS, [], [], MARKETS, normal, rules, timedelta(0))
    with pytest.raises(SimulationError):
        simulate(BARS, [], [], MARKETS, normal, rules, MINUTE, initial_cash=math.inf)
    with pytest.raises(SimulationError):
        fill_rules(
            FillScenario.STRESS,
            {"stress": {"latency_seconds": 1, "limit_fill_fraction": 0, "impact_multiplier": 1}},
        )


# ---------------------------------------------------------------- funding inside a limit fill's bar
DAY = timedelta(days=1)
DAILY = [
    Bar("BTC", utc(2026, 1, 1), 100.0, 100.0, 100.0, 100.0),
    Bar("BTC", utc(2026, 1, 2), 100.0, 101.0, 99.0, 100.0),
    Bar("BTC", utc(2026, 1, 3), 100.0, 100.0, 100.0, 100.0),
]


def _daily(orders: list[Order], rate: float) -> Result:
    # one settlement at 08:00 inside the 2 January bar, mark price 100
    funding = [FundingEvent("BTC", utc(2026, 1, 2, 8), rate=rate, mark_price=100.0)]
    return simulate(
        DAILY,
        orders,
        funding,
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        DAY,
    )


def test_a_limit_fill_is_not_assumed_to_beat_funding_inside_its_bar() -> None:
    # A sell limit at 100.5 fills in the 2 January bar (high 101), at an unknown time of day.
    # With +1% funding at 08:00 a short receives 10 x 100 x 1% = 10.0 if it was already short,
    # nothing if it was not: the worse of the two, nothing, is booked.
    sell = Order(utc(2026, 1, 1), "BTC", -10.0, OrderKind.LIMIT, limit_price=100.5)
    r = _daily([sell], 0.01)
    assert len(r.fills) == 1
    assert r.funding == []
    # with -1% funding the short would pay 10.0 if it was already short: that is booked
    (paid,) = _daily([sell], -0.01).funding
    assert (paid.position, paid.cash) == (-10.0, pytest.approx(-10.0))


def test_a_limit_fill_that_closes_a_position_does_not_dodge_funding() -> None:
    # Long 10 from the 1 January open (a market order decided at 23:58, active at 23:59), then a
    # sell limit at 100.5 closes it somewhere in the 2 January bar. +1% funding at 08:00: the long
    # pays 10.0 if it was still open, nothing if not; the worse, paying, is booked.
    buy = Order(utc(2025, 12, 31, 23, 58), "BTC", 10.0)
    close = Order(utc(2026, 1, 1), "BTC", -10.0, OrderKind.LIMIT, limit_price=100.5)
    r = _daily([buy, close], 0.01)
    assert [f.liquidity for f in r.fills] == [Liquidity.TAKER, Liquidity.MAKER]
    assert r.positions == {"BTC": 0.0}
    (paid,) = r.funding
    assert (paid.position, paid.cash) == (10.0, pytest.approx(-10.0))


def test_a_market_fill_at_the_open_is_certain_before_funding() -> None:
    # a market order fills at the bar's open, before 08:00 for sure: the short receives 10.0
    sell = Order(utc(2025, 12, 31, 23, 58), "BTC", -10.0)
    (paid,) = _daily([sell], 0.01).funding
    assert (paid.position, paid.cash) == (-10.0, pytest.approx(10.0))


def test_funding_at_the_end_of_a_limit_fills_bar_counts_the_fill() -> None:
    # a settlement exactly when the fill's bar ends comes after the fill, whenever in the bar
    sell = Order(utc(2026, 1, 1), "BTC", -10.0, OrderKind.LIMIT, limit_price=100.5)
    funding = [FundingEvent("BTC", utc(2026, 1, 3), rate=0.01, mark_price=100.0)]
    r = simulate(
        DAILY,
        [sell],
        funding,
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        DAY,
    )
    (paid,) = r.funding
    assert (paid.position, paid.cash) == (-10.0, pytest.approx(10.0))


# ---------------------------------------------------------------- the default longest wait
def _flat(minutes: list[int], price: float = 100.0) -> list[Bar]:
    return [Bar("BTC", t(m), price, price + 1.0, price - 1.0, price) for m in minutes]


def test_an_order_without_an_expiry_waits_at_most_max_wait_bars() -> None:
    # config: max_wait_bars = 5. A market buy decided at 10:01 is active at 10:02, so it may fill
    # on a bar starting by 10:02 + 5 x 1 min = 10:07. No trades (no bars) from 10:02 to 10:07:
    rules = fill_rules(FillScenario.CONSERVATIVE)
    assert rules.max_wait_bars == 5
    buy = Order(t(1), "BTC", 1.0)
    # the next bar starts at 10:08: too late, cancelled
    assert _conservative_on(_flat([0, 1, 8]), [buy]).unfilled == [0]
    # a bar at 10:07 is just in time: fills at its open
    assert [f.at for f in _conservative_on(_flat([0, 1, 7, 8]), [buy]).fills] == [t(7)]


def test_a_resting_limit_order_without_an_expiry_needs_its_bar_to_end_in_time() -> None:
    # buy limit 99.5 decided 10:00, active 10:01, expires 10:06. Every bar trades through it
    # (low 99) but none opens through it (open 100). The 10:05 bar ends at 10:06: fills.
    # With no bars from 10:01 to 10:05, the 10:06 bar ends at 10:07: too late, cancelled.
    order = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, limit_price=99.5)
    r = _conservative_on(_flat([0, 5, 6]), [order])
    assert [(f.at, f.price, f.liquidity) for f in r.fills] == [(t(5), 99.5, Liquidity.MAKER)]
    assert _conservative_on(_flat([0, 6]), [order]).unfilled == [0]


def test_an_expiry_on_the_order_overrides_the_default_wait() -> None:
    # the market buy from above, good until 10:30: the 10:08 bar is now in time
    buy = Order(t(1), "BTC", 1.0, expires_at=t(30))
    assert [f.at for f in _conservative_on(_flat([0, 1, 8]), [buy]).fills] == [t(8)]


def test_the_wait_is_counted_in_bars_and_from_the_active_instant() -> None:
    # daily bars: decided 1 Jan 00:00, active 00:01 (conservative latency 60 s), so a market
    # order may fill on a bar starting by 6 Jan 00:01. Bars on 1 Jan and 6 Jan: fills on 6 Jan.
    day = [Bar("BTC", utc(2026, 1, d), 100.0, 101.0, 99.0, 100.0) for d in (1, 6, 7)]
    buy = Order(utc(2026, 1, 1), "BTC", 1.0)
    rules = fill_rules(FillScenario.CONSERVATIVE)
    r = simulate(day, [buy], [], MARKETS, cost_model(Scenario.NORMAL), rules, DAY)
    assert [f.at for f in r.fills] == [utc(2026, 1, 6)]
    # without the 6 Jan bar the next one, 7 Jan 00:00, starts after 6 Jan 00:01: cancelled
    r = simulate(day[:1] + day[2:], [buy], [], MARKETS, cost_model(Scenario.NORMAL), rules, DAY)
    assert r.unfilled == [0]


def test_without_max_wait_bars_an_order_waits_for_ever() -> None:
    cfg = {
        "conservative": {"latency_seconds": 60, "limit_fill_fraction": 1, "impact_multiplier": 1}
    }
    rules = fill_rules(FillScenario.CONSERVATIVE, cfg)
    assert rules.max_wait_bars is None
    r = simulate(
        _flat([0, 1, 50]),
        [Order(t(1), "BTC", 1.0)],
        [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        rules,
        MINUTE,
    )
    assert [f.at for f in r.fills] == [t(50)]
    with pytest.raises(SimulationError):
        fill_rules(
            FillScenario.CONSERVATIVE, {"conservative": cfg["conservative"] | {"max_wait_bars": 0}}
        )
