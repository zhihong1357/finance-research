import math
import random
from statistics import fmean, stdev

import pytest

from finresearch.meta.bench import (
    BenchError,
    Split,
    Variant,
    bench_config,
    cohort_returns,
    daily_pnl,
    family,
    portfolio,
    rank_weights,
    rates,
    run_family,
    run_many,
    split,
)
from finresearch.meta.noise import noise_model

SMALL = {
    "instruments": 8,
    "days": 600,
    "lookbacks": (1, 5),
    "holdings": (1,),
    "embargo": 5,
    "partitions": 4,
    "restarts": 3,
    "hypotheses": 6,
}


# ---------------------------------------------------------------- the pieces by hand
def test_rank_weights_by_hand() -> None:
    # day 0, lookback 1: signals 0.3, -0.1, 0.2 rank 3, 1, 2; centre 2; demeaned 1, -1, 0;
    # gross |1| + |-1| + |0| = 2: weights 0.5, -0.5, 0
    base = [[0.3, 0.1], [-0.1, 0.5], [0.2, 0.0]]
    w = rank_weights(base, 1)
    assert w[0] == pytest.approx([0.5, -0.5, 0.0])
    # lookback 2 on day 1: sums 0.4, 0.4, 0.2 -> the tie goes by instrument order: ranks 2, 3, 1
    w2 = rank_weights(base, 2)
    assert w2[0] is None
    assert w2[1] == pytest.approx([0.0, 0.5, -0.5])


def test_portfolio_averages_the_live_cohorts() -> None:
    p = portfolio([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]], 2)
    assert p[0] is None
    assert p[1] == pytest.approx([0.5, 0.5])
    assert p[2] == pytest.approx([0.5, 1.0])


def test_daily_pnl_by_hand() -> None:
    # positions held from the end of day t - 1; the cost is paid on the change from t - 2
    positions = [None, [0.5, -0.5], [0.5, -0.5], [-0.5, 0.5], [-0.5, 0.5]]
    returns = [[0.0, 0.0, 0.01, 0.02, 0.03], [0.0, 0.0, -0.01, 0.0, 0.01]]
    gross, costs = daily_pnl(returns, positions, 0.001)
    assert gross[2] == 0.0  # the day before the first change has no earlier position
    assert gross[3] == pytest.approx(0.5 * 0.02 - 0.5 * 0.0)
    assert costs[3] == 0.0
    assert gross[4] == pytest.approx(-0.5 * 0.03 + 0.5 * 0.01)
    assert costs[4] == pytest.approx(0.001 * 2.0)  # |-0.5 - 0.5| + |0.5 + 0.5|


def test_cohort_returns_by_hand() -> None:
    # weights 0.5, -0.5 every day, holding 2, first confirmation day 1, end 6, cost 0.001:
    # formed at 0: days 1-2 earn 0.5 x (0.01 + 0.02) = 0.015, enter gross 1: -0.001 -> 0.014
    # formed at 2: days 3-4 earn 0.5 x (0.03 + 0.04) = 0.035, same weights: no cost
    # formed at 4 would need day 6: stop
    weights: list[list[float] | None] = [[0.5, -0.5]] * 6
    returns = [[0.0, 0.01, 0.02, 0.03, 0.04, 0.05], [0.0] * 6]
    assert cohort_returns(returns, weights, 2, 1, 0.001, 1, 6) == pytest.approx([0.014, 0.035])
    assert cohort_returns(returns, weights, 2, -1, 0.0, 1, 6) == pytest.approx([-0.015, -0.035])


def test_split_by_hand() -> None:
    cfg = bench_config(days=100, embargo=5, holdings=(1, 5))
    assert split(cfg, 10) == Split(10, 60, 65, 85)
    with pytest.raises(BenchError):
        split(bench_config(days=20, embargo=5, holdings=(1,)), 15)


def test_the_family_is_the_grid() -> None:
    cfg = bench_config()
    variants = family(cfg)
    assert len(variants) == 6 * 3 * 2  # decision 0024: 36 variants
    assert str(variants[0]) == "momentum L1 H1"
    assert str(variants[1]) == "reversal L1 H1"
    assert Variant(20, 5, -1) in variants


# ---------------------------------------------------------------- the bench's own checks
def test_the_confirmation_is_a_fresh_test() -> None:
    # Selection happens on the exploration segment only, so on noise without costs the kept
    # variant's confirmation t statistic is standard normal (holding 1: both t statistics are
    # the ordinary one). Over 60 small worlds: mean within 3 / sqrt(60) of 0, sd within 25% of
    # 1, while the kept variant's exploration Sharpe ratio is positive every time (it was chosen
    # for that). With costs the mean is negative (about -0.45 here): noise loses what it trades.
    cfg = bench_config(**SMALL, cost=0.0)
    model = noise_model()
    rng = random.Random(20261118)
    trials = [run_family(cfg, model, rng) for _ in range(60)]
    t = [r.t_nonoverlapping for r in trials]
    assert abs(fmean(t)) < 3 / math.sqrt(60)
    assert stdev(t) == pytest.approx(1.0, rel=0.25)
    assert all(r.exploration_sharpe > 0.0 for r in trials)
    assert all(r.effective_trials >= 2 for r in trials)


def test_overlapping_holdings_show_the_newey_west_shortfall() -> None:
    # Holding 5 only: on noise the non-overlapping t is standard normal, while Newey-West with
    # lag 4 is too wide by about sqrt(75 / 51) = 1.21 (stats.hac), seen here through the whole
    # pipeline. 60 small worlds.
    cfg = bench_config(**{**SMALL, "holdings": (5,)}, cost=0.0)
    model = noise_model()
    rng = random.Random(5)
    trials = [run_family(cfg, model, rng) for _ in range(60)]
    assert stdev([r.t_nonoverlapping for r in trials]) == pytest.approx(1.0, rel=0.25)
    assert stdev([r.t_newey_west for r in trials]) > stdev([r.t_nonoverlapping for r in trials])


def test_a_strong_planted_feature_is_found_and_supported() -> None:
    cfg = bench_config(**SMALL)
    model = noise_model()
    rng = random.Random(9)
    for _ in range(3):
        r = run_many(cfg, model, rng, strength=0.3)
        assert r.selected_index == 0
        assert r.verdict_newey_west.passed
        assert r.verdict_nonoverlapping.passed


def test_rates_count_each_gate() -> None:
    cfg = bench_config(**SMALL)
    model = noise_model()
    rng = random.Random(3)
    trials = [run_many(cfg, model, rng, strength=0.3) for _ in range(2)] + [
        run_many(cfg, model, rng) for _ in range(2)
    ]
    r = rates(trials)
    assert r["pipeline (non-overlapping)"].hits >= 2
    assert r["pipeline (non-overlapping)"].n == 4
    low, high = r["deflated Sharpe"].interval
    assert 0.0 <= low <= r["deflated Sharpe"].share <= high <= 1.0


def test_invalid_settings_are_refused() -> None:
    bad = [
        {"instruments": 1},
        {"exploration": 0.8, "confirmation": 0.3},
        {"lookbacks": ()},
        {"holdings": (0,)},
        {"embargo": 2, "holdings": (1, 5)},
        {"cost": -0.1},
    ]
    for override in bad:
        with pytest.raises(BenchError):
            bench_config(**override)
    with pytest.raises(BenchError):
        rates([])
