import math
from datetime import timedelta

import pytest

from finresearch.sim.costs import (
    CostError,
    Fees,
    Liquidity,
    Scenario,
    all_scenarios,
    cost_model,
    fee,
    funding_cash,
    impact_cost,
    impact_fraction,
    spread_cost,
)

BINANCE = Fees(maker=0.0002, taker=0.0005)
# A $10,000 trade in a contract that trades $100m a day with 4% daily volatility, 1 bp half spread
TRADE = {"half_spread": 0.0001, "daily_volume": 100_000_000.0, "daily_vol": 0.04}


def test_fees_by_hand() -> None:
    assert fee(10_000.0, BINANCE, Liquidity.TAKER) == pytest.approx(5.0)  # 10,000 x 0.05%
    assert fee(-10_000.0, BINANCE, Liquidity.MAKER) == pytest.approx(2.0)  # sells pay too
    assert fee(0.0, BINANCE, Liquidity.TAKER) == 0.0


def test_spread_by_hand() -> None:
    assert spread_cost(10_000.0, 0.0001) == pytest.approx(1.0)  # 10,000 x 1 bp
    assert spread_cost(-10_000.0, 0.0001) == pytest.approx(1.0)


def test_square_root_impact_by_hand() -> None:
    # sqrt(10,000 / 100,000,000) = 0.01; 1.0 x 0.04 x 0.01 = 0.0004 of price; 10,000 x 0.0004 = 4.0
    assert impact_fraction(10_000.0, 100_000_000.0, 0.04, 1.0) == pytest.approx(0.0004)
    assert impact_cost(10_000.0, 100_000_000.0, 0.04, 1.0) == pytest.approx(4.0)
    # four times the size: twice the impact per dollar, eight times the cost
    assert impact_fraction(40_000.0, 100_000_000.0, 0.04, 1.0) == pytest.approx(0.0008)
    assert impact_cost(40_000.0, 100_000_000.0, 0.04, 1.0) == pytest.approx(32.0)


def test_a_metaorder_pays_for_its_whole_size() -> None:
    # 10,000 after 30,000 already traded: total 40,000 costs 32.0 (above), the 30,000 before it
    # 30,000 x 0.04 x sqrt(30,000 / 1e8) = 1200 x 0.0173205 = 20.7846; the increase 11.2154
    assert impact_cost(10_000.0, 1e8, 0.04, 1.0, prior=30_000.0) == pytest.approx(11.215390, 1e-6)
    # four slices of 10,000 add up to one trade of 40,000
    slices = [impact_cost(10_000.0, 1e8, 0.04, 1.0, prior=10_000.0 * k) for k in range(4)]
    assert math.fsum(slices) == pytest.approx(impact_cost(40_000.0, 1e8, 0.04, 1.0))
    # the sign of the slice does not matter, only its size
    assert impact_cost(-10_000.0, 1e8, 0.04, 1.0, prior=30_000.0) == pytest.approx(11.215390, 1e-6)
    assert cost_model(Scenario.NORMAL).metaorder_window == timedelta(hours=24)


def test_funding_by_hand() -> None:
    # $10,000 long through a +0.01% settlement pays 1.0; the short receives it
    assert funding_cash(10_000.0, 0.0001) == pytest.approx(-1.0)
    assert funding_cash(-10_000.0, 0.0001) == pytest.approx(1.0)
    # a negative rate reverses who pays
    assert funding_cash(10_000.0, -0.0001) == pytest.approx(1.0)
    assert funding_cash(0.0, 0.0003) == 0.0


def test_a_trade_by_hand_in_each_scenario() -> None:
    # Binance taker: fee 5.0 + spread 1.0 + impact 4.0 = 10.0 under normal costs
    models = all_scenarios()
    costs = {s: m.trade("binance", 10_000.0, Liquidity.TAKER, **TRADE) for s, m in models.items()}
    assert costs[Scenario.NORMAL].fee == pytest.approx(5.0)
    assert costs[Scenario.NORMAL].spread == pytest.approx(1.0)
    assert costs[Scenario.NORMAL].impact == pytest.approx(4.0)
    assert costs[Scenario.NORMAL].total == pytest.approx(10.0)
    assert costs[Scenario.DOUBLE].total == pytest.approx(20.0)
    assert costs[Scenario.ZERO].total == 0.0
    # Bybit taker: fee 5.5 + 1.0 + 4.0
    bybit = models[Scenario.NORMAL].trade("bybit", 10_000.0, Liquidity.TAKER, **TRADE)
    assert bybit.total == pytest.approx(10.5)


def test_the_config_holds_the_published_rates() -> None:
    normal = cost_model(Scenario.NORMAL)
    assert normal.fees["binance"] == BINANCE
    assert normal.fees["bybit"] == Fees(maker=0.0002, taker=0.00055)
    assert normal.multiplier == 1.0


def test_a_config_can_be_passed_in() -> None:
    cfg = {
        "fees": {"x": {"maker": 0.0, "taker": 0.001}},
        "impact": {"y": 0.5, "metaorder_hours": 24},
        "scenarios": {"zero": 0.0, "normal": 1.0, "double": 2.0},
    }
    m = cost_model(Scenario.DOUBLE, cfg)
    cost = m.trade("x", 10_000.0, Liquidity.TAKER, **TRADE)
    # fee 2 x 10.0, spread 2 x 1.0, impact 2 x 0.5 x 4.0
    assert (cost.fee, cost.spread, cost.impact) == (
        pytest.approx(20.0),
        pytest.approx(2.0),
        pytest.approx(4.0),
    )


def test_invalid_inputs_are_refused() -> None:
    bad_calls = [
        lambda: Fees(maker=-0.0001, taker=0.0005),
        lambda: Fees(maker=0.0002, taker=1.0),
        lambda: spread_cost(10_000.0, -0.0001),
        lambda: impact_fraction(10_000.0, 0.0, 0.04, 1.0),
        lambda: impact_fraction(10_000.0, 1e8, -0.04, 1.0),
        lambda: funding_cash(10_000.0, math.nan),
        lambda: impact_cost(10_000.0, 1e8, 0.04, 1.0, prior=-1.0),
        lambda: cost_model(
            Scenario.NORMAL,
            {"fees": {}, "impact": {"y": 1.0, "metaorder_hours": 0}, "scenarios": {"normal": 1.0}},
        ),
        lambda: cost_model(Scenario.NORMAL).trade("okx", 1.0, Liquidity.TAKER, **TRADE),
        lambda: cost_model(
            Scenario.ZERO,
            {"fees": {}, "impact": {"y": 1.0}, "scenarios": {"zero": -1.0}},
        ),
    ]
    for call in bad_calls:
        with pytest.raises(CostError):
            call()
