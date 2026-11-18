import math
import random

import pytest

from finresearch.stats.montecarlo import mean
from finresearch.stats.sharpe import (
    EULER_GAMMA,
    SharpeError,
    deflated_sharpe,
    expected_max_sharpe,
    min_track_record,
    moments,
    probabilistic_sharpe,
    sharpe_standard_error,
)


def test_moments_by_hand() -> None:
    # x = 1, 2, 3, 4, 10: mean 4, deviations -3, -2, -1, 0, 6
    # m2 = 50 / 5 = 10, m3 = 180 / 5 = 36, m4 = 1394 / 5 = 278.8
    # skew = 36 / 10^1.5 = 1.138420, kurt = 278.8 / 100 = 2.788
    # sample std = sqrt(50 / 4) = 3.535534, SR = 4 / 3.535534 = 1.131371
    m = moments([1.0, 2.0, 3.0, 4.0, 10.0])
    assert (m.n, m.mean) == (5, 4.0)
    assert m.std == pytest.approx(math.sqrt(12.5))
    assert m.skew == pytest.approx(36 / 10**1.5)
    assert m.kurt == pytest.approx(2.788)
    assert m.sharpe == pytest.approx(1.131371, abs=1e-6)


def test_standard_error_and_probabilistic_sharpe_by_hand() -> None:
    # SR 0.1, n 101, normal (skew 0, kurt 3): se = sqrt((1 + 0.5 * 0.01) / 100) = 0.100250
    # PSR(0) = Phi(0.1 / 0.100250) = Phi(0.997506) = 0.840741
    assert sharpe_standard_error(0.1, 101, 0.0, 3.0) == pytest.approx(math.sqrt(1.005 / 100))
    assert probabilistic_sharpe(0.1, 0.0, 101, 0.0, 3.0) == pytest.approx(0.840741, abs=1e-6)
    assert probabilistic_sharpe(0.1, 0.1, 101, 0.0, 3.0) == 0.5


def test_negative_skew_and_fat_tails_widen_the_error() -> None:
    normal = sharpe_standard_error(0.1, 500, 0.0, 3.0)
    assert sharpe_standard_error(0.1, 500, -1.0, 3.0) > normal
    assert sharpe_standard_error(0.1, 500, 0.0, 10.0) > normal
    assert sharpe_standard_error(0.1, 500, 1.0, 3.0) < normal


def test_min_track_record_by_hand() -> None:
    # SR 0.1, benchmark 0, normal, 95%: z = 1.644854; 1 + 1.005 * (1.644854 / 0.1)^2 = 272.907
    trl = min_track_record(0.1, 0.0, 0.0, 3.0, 0.95)
    assert trl == pytest.approx(272.907, abs=1e-3)
    # by construction, PSR with exactly that many observations is the confidence asked for
    se = math.sqrt(1.005 / (trl - 1.0))
    assert 0.1 / se == pytest.approx(1.644854, abs=1e-6)


def test_expected_max_sharpe_by_hand() -> None:
    # N = 100, V = 1: (1 - g) Phi^-1(0.99) + g Phi^-1(1 - 1 / (100 e))
    # = 0.422784 * 2.326348 + 0.577216 * 2.680210 = 0.983545 + 1.547059 = 2.530604
    assert math.isclose(EULER_GAMMA, 0.5772156649, abs_tol=1e-10)
    assert expected_max_sharpe(100, 1.0) == pytest.approx(2.530603, abs=1e-5)
    assert expected_max_sharpe(100, 4.0) == pytest.approx(2 * 2.530603, abs=1e-5)
    assert expected_max_sharpe(100, 0.0) == 0.0


def test_one_trial_or_a_few_are_never_deflated_below_none() -> None:
    # The best of N >= 1 draws is never expected below 0, the value at N = 1. The approximation
    # turns negative below about 1.29 trials (at 1.2: 0.422784 * -0.967422 + 0.577216 * 0.506080
    # = -0.117166), so it is floored at 0. N = 2: 0.577216 * Phi^-1(1 - 1 / (2 e)) = 0.577216 *
    # 0.900453 = 0.519755, a little below the exact 1 / sqrt(pi) = 0.564190.
    assert expected_max_sharpe(1, 1.0) == 0.0
    assert expected_max_sharpe(1.2, 1.0) == 0.0
    assert expected_max_sharpe(2, 1.0) == pytest.approx(0.519755, abs=1e-6)
    sr, variance = 0.05, 0.002
    psr = probabilistic_sharpe(sr, 0.0, 500, 0.0, 3.0)
    for trials in (1.0, 1.01, 1.1, 1.2, 1.28):
        assert deflated_sharpe(sr, 500, 0.0, 3.0, trials, variance) == psr
    assert deflated_sharpe(sr, 500, 0.0, 3.0, 1.5, variance) < psr


def test_expected_max_sharpe_is_close_to_the_simulated_maximum() -> None:
    # The best of 100 standard normal draws averages about 2.5076; the approximation gives 2.5306.
    rnd = random.Random(20261118)
    best = mean([max(rnd.gauss() for _ in range(100)) for _ in range(4000)])
    assert abs(expected_max_sharpe(100, 1.0) - best.value) / best.value < 0.02
    assert best.standard_error < 0.01


def test_deflated_sharpe_matches_the_paper() -> None:
    # Bailey & López de Prado (2014): daily strategy, annualised SR 2.5 found after 100 trials,
    # 5 years (1250 days), variance of the trials' annualised SR 0.5, skew -3, kurtosis 10:
    # DSR = 0.9004; had the strategy been found after 46 trials, 0.9505.
    sr, variance = 2.5 / math.sqrt(250), 0.5 / 250
    assert expected_max_sharpe(100, variance) == pytest.approx(0.1132, abs=1e-4)
    assert deflated_sharpe(sr, 1250, -3.0, 10.0, 100, variance) == pytest.approx(0.9004, abs=1e-4)
    assert deflated_sharpe(sr, 1250, -3.0, 10.0, 46, variance) == pytest.approx(0.9505, abs=1e-4)


def test_more_trials_or_more_dispersion_deflate_more() -> None:
    sr, variance = 0.1, 0.002
    by_trials = [deflated_sharpe(sr, 1000, 0.0, 3.0, n, variance) for n in (2, 10, 100, 1000)]
    assert by_trials == sorted(by_trials, reverse=True)
    by_var = [deflated_sharpe(sr, 1000, 0.0, 3.0, 100, v) for v in (0.0, 0.001, 0.004)]
    assert by_var == sorted(by_var, reverse=True)
    # with no dispersion among trials, deflation is just PSR against zero
    assert by_var[0] == probabilistic_sharpe(sr, 0.0, 1000, 0.0, 3.0)
    # fractional effective trials are allowed and sit between their neighbours
    assert by_trials[1] > deflated_sharpe(sr, 1000, 0.0, 3.0, 10.5, variance) > by_trials[2]


def test_invalid_inputs_are_refused() -> None:
    bad_calls = [
        lambda: moments([1.0]),
        lambda: moments([1.0, 1.0, 1.0]),
        lambda: moments([0.1, 0.1, 0.1]),  # the mean rounds away from 0.1: tested on the inputs
        lambda: moments([1.0, math.nan]),
        lambda: sharpe_standard_error(0.1, 1, 0.0, 3.0),
        lambda: sharpe_standard_error(2.0, 100, 1.0, 1.0),  # 1 - 2 + 0 <= 0
        lambda: min_track_record(0.1, 0.1, 0.0, 3.0, 0.95),
        lambda: min_track_record(0.1, 0.0, 0.0, 3.0, 1.0),
        lambda: expected_max_sharpe(0.5, 1.0),
        lambda: expected_max_sharpe(math.nan, 1.0),
        lambda: expected_max_sharpe(math.inf, 1.0),
        lambda: expected_max_sharpe(10, -1.0),
        lambda: expected_max_sharpe(10, math.nan),
    ]
    for call in bad_calls:
        with pytest.raises(SharpeError):
            call()
