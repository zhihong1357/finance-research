import math
from datetime import timedelta

import pytest

from finresearch.core.time import utc
from finresearch.sim.capacity import Capacity, CapacityError, Strategy, capacity, net_result
from finresearch.sim.costs import Scenario, cost_model
from finresearch.sim.engine import Bar, FillScenario, Market, Order, fill_rules, simulate

# 0.3% gross per period, 2 trades of the whole capital, 0.06% linear cost (0.05% taker + 1 bp),
# square-root impact y = 1 at 4% daily volatility in a market trading $100m a day
STRATEGY = Strategy(
    gross_edge=0.003,
    trades=2.0,
    trade_size=1.0,
    linear_cost=0.0006,
    y=1.0,
    daily_vol=0.04,
    daily_volume=100_000_000.0,
)


def test_capacity_by_hand() -> None:
    # a = 0.003 - 2 x 0.0006 = 0.0018; b = 2 x 1 x 0.04 x 1 / sqrt(1e8) = 8e-6
    assert STRATEGY.a == pytest.approx(0.0018)
    assert STRATEGY.b == pytest.approx(8e-6)
    # break-even (a / b)^2 = 225^2 = 50,625; best (2a / 3b)^2 = 150^2 = 22,500
    # net at best: 0.0018 x 22,500 - 8e-6 x 22,500^1.5 = 40.5 - 27 = 13.5 (= a x best / 3)
    cap = capacity(STRATEGY)
    assert cap.break_even == pytest.approx(50_625.0)
    assert cap.best == pytest.approx(22_500.0)
    assert cap.best_net == pytest.approx(13.5)
    assert cap.best == pytest.approx(4 / 9 * cap.break_even)


def test_net_result_by_hand() -> None:
    assert net_result(STRATEGY, 0.0) == 0.0
    assert net_result(STRATEGY, 50_625.0) == pytest.approx(0.0, abs=1e-9)
    # 10,000: 18 - 8e-6 x 1e6 = 10; 100,000: 180 - 8e-6 x 3.16228e7 = 180 - 252.98 = -72.98
    assert net_result(STRATEGY, 10_000.0) == pytest.approx(10.0)
    assert net_result(STRATEGY, 100_000.0) == pytest.approx(180 - 8e-6 * 100_000**1.5)


def test_no_edge_after_linear_costs_means_no_capacity() -> None:
    thin = Strategy(0.001, 2.0, 1.0, 0.0006, 1.0, 0.04, 1e8)  # a = 0.001 - 0.0012 < 0
    assert capacity(thin) == Capacity(0.0, 0.0, 0.0)
    free = Strategy(0.003, 2.0, 1.0, 0.0006, 0.0, 0.04, 1e8)  # no impact at all
    assert capacity(free).break_even == math.inf


def test_more_liquidity_or_less_turnover_raises_capacity() -> None:
    base = capacity(STRATEGY).break_even
    deeper = Strategy(0.003, 2.0, 1.0, 0.0006, 1.0, 0.04, 400_000_000.0)
    assert capacity(deeper).break_even == pytest.approx(4 * base)  # break-even scales with volume
    calmer = Strategy(0.003, 2.0, 1.0, 0.0006, 1.0, 0.02, 100_000_000.0)
    assert capacity(calmer).break_even == pytest.approx(4 * base)  # and with 1 / vol^2


def test_invalid_strategies_are_refused() -> None:
    bad = [
        {"trades": 0.0},
        {"trade_size": -1.0},
        {"linear_cost": -0.001},
        {"daily_volume": 0.0},
        {"y": -1.0},
    ]
    fields = {
        "gross_edge": 0.003,
        "trades": 2.0,
        "trade_size": 1.0,
        "linear_cost": 0.0006,
        "y": 1.0,
        "daily_vol": 0.04,
        "daily_volume": 1e8,
    }
    for change in bad:
        with pytest.raises(CapacityError):
            Strategy(**(fields | change))
    with pytest.raises(CapacityError):
        net_result(STRATEGY, -1.0)


# ---------------------------------------------------------------- against the simulator
ROUND_TRIPS = 5
LOW, HIGH = 100.0, 100.3  # buy at 100, sell at 100.3: 0.3% gross per round trip


def _round_trips(capital: float) -> float:
    """Simulated costs of ROUND_TRIPS buy-low, sell-high round trips with ``capital``."""
    opens = [LOW if m % 2 == 0 else HIGH for m in range(2 * ROUND_TRIPS + 2)]
    bars = []
    for m, o in enumerate(opens):
        c = opens[m + 1] if m + 1 < len(opens) else o
        bars.append(Bar("BTC", utc(2026, 1, 1, 10, m), o, max(o, c), min(o, c), c))
    quantity = capital / LOW
    orders = []
    for j in range(1, ROUND_TRIPS + 1):  # decided a minute ahead; latency 60 s
        orders.append(Order(utc(2026, 1, 1, 10, 2 * j - 1), "BTC", quantity))  # fills at bar 2j
        orders.append(Order(utc(2026, 1, 1, 10, 2 * j), "BTC", -quantity))  # fills at bar 2j + 1
    markets = {"BTC": Market("binance", 0.0001, 100_000_000.0, 0.04)}
    rules = fill_rules(FillScenario.CONSERVATIVE)
    result = simulate(
        bars, orders, [], markets, cost_model(Scenario.NORMAL), rules, timedelta(minutes=1)
    )
    assert len(result.fills) == 2 * ROUND_TRIPS
    assert result.positions["BTC"] == pytest.approx(0.0, abs=1e-12)
    gross = ROUND_TRIPS * quantity * (HIGH - LOW)
    assert result.cash == pytest.approx(gross - result.costs)
    return result.costs


@pytest.mark.parametrize("capital", [10_000.0, 50_625.0, 100_000.0])
def test_the_simulator_charges_what_the_formula_assumes(capital: float) -> None:
    # The formula counts each trade at the starting capital; the simulator's sells are 0.3% larger,
    # so its costs run a few tenths of a percent higher.
    formula = ROUND_TRIPS * (STRATEGY.gross_edge * capital - net_result(STRATEGY, capital))
    assert _round_trips(capital) == pytest.approx(formula, rel=5e-3)


def test_below_break_even_wins_and_above_it_loses_in_the_simulator() -> None:
    def net(capital: float) -> float:
        return ROUND_TRIPS * capital / LOW * (HIGH - LOW) - _round_trips(capital)

    assert net(10_000.0) > 0.0
    assert net(100_000.0) < 0.0
    assert net(22_500.0) > net(10_000.0)  # near the best size the money made is larger
