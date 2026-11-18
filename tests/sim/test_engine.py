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
    # The limit sell is active from 10:03: highs 102.8 and 102.2 never trade through 102.9,
    # and at 10:05 it has expired.
    assert r.unfilled == [1]
    # Long 2 at the 10:03 funding: pays 2 x 102 x 0.01% = 0.0204
    (paid,) = r.funding
    assert (paid.position, paid.cash) == (2.0, pytest.approx(-0.0204))
    # Cash: -203 - 0.1449385 - 0.0204 = -203.1653385; 2 x last close 100.8 = 201.6
    assert r.cash == pytest.approx(-203.1653385, abs=1e-7)
    assert r.equity[-1][1] == pytest.approx(-1.5653385, abs=1e-7)


def test_one_call_gives_every_scenario() -> None:
    results = simulate_all(BARS, ORDERS, FUNDING, MARKETS, MINUTE)
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
    # Stress is never better than conservative under the same costs
    for c in Scenario:
        assert equity[(c, FillScenario.STRESS)] <= equity[(c, FillScenario.CONSERVATIVE)]


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


def test_a_gap_does_not_improve_a_limit_fill() -> None:
    # buy limit at 101.2 active from 10:01: that bar opens at 101 (better) but fills at 101.2
    order = Order(t(0), "BTC", 1.0, OrderKind.LIMIT, limit_price=101.2)
    r = simulate(
        BARS,
        [order],
        [],
        MARKETS,
        cost_model(Scenario.NORMAL),
        fill_rules(FillScenario.CONSERVATIVE),
        MINUTE,
    )
    assert [(f.at, f.price) for f in r.fills] == [(t(1), 101.2)]


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
    ]
    for bars, orders in bad_runs:
        with pytest.raises(SimulationError):
            simulate(bars, orders, [], MARKETS, normal, rules, MINUTE)
    with pytest.raises(SimulationError):
        simulate(BARS, [], [], MARKETS, normal, rules, timedelta(0))
    with pytest.raises(SimulationError):
        fill_rules(
            FillScenario.STRESS,
            {"stress": {"latency_seconds": 1, "limit_fill_fraction": 0, "impact_multiplier": 1}},
        )
