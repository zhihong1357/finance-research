import math
import random

import pytest

from finresearch.stats.maxima import (
    MaximaError,
    cholesky,
    equivalent_trials,
    expected_max_iid,
    expected_max_normal,
    null_benchmark,
)


def test_cholesky_by_hand() -> None:
    # [[4, 2], [2, 3]]: l11 = 2, l21 = 1, l22 = sqrt(3 - 1) = sqrt(2)
    low = cholesky([[4.0, 2.0], [2.0, 3.0]])
    assert low == [[2.0, 0.0], [1.0, pytest.approx(math.sqrt(2.0))]]
    # rank deficient: a trial and its mirror image (correlation -1) need one direction
    assert cholesky([[1.0, -1.0], [-1.0, 1.0]]) == [[1.0, 0.0], [-1.0, 0.0]]


def test_the_exact_iid_maximum_by_hand() -> None:
    # n = 1: 0; n = 2: 1 / sqrt(pi) = 0.564190; n = 3: 3 / (2 sqrt(pi)) = 0.846284
    assert expected_max_iid(1.0) == 0.0
    assert expected_max_iid(2.0) == pytest.approx(1.0 / math.sqrt(math.pi), abs=1e-9)
    assert expected_max_iid(3.0) == pytest.approx(3.0 / (2.0 * math.sqrt(math.pi)), abs=1e-9)
    # and the count that matches it comes back
    assert equivalent_trials(1.0 / math.sqrt(math.pi)) == pytest.approx(2.0, rel=1e-6)
    assert equivalent_trials(expected_max_iid(20.0)) == pytest.approx(20.0, rel=1e-6)
    assert equivalent_trials(-0.1) == 1.0


def test_simulated_maxima_match_the_exact_ones() -> None:
    rng = random.Random(1)
    # independent: E[max of 5] exactly 1.162964
    est = expected_max_normal(
        [[1.0 if i == j else 0.0 for j in range(5)] for i in range(5)], 20_000, rng
    )
    assert abs(est.value - expected_max_iid(5.0)) < 4 * est.standard_error
    assert expected_max_iid(5.0) == pytest.approx(1.162964, abs=1e-6)
    # perfectly correlated: one trial, maximum 0
    est = expected_max_normal([[1.0] * 4 for _ in range(4)], 5_000, rng)
    assert est.value == pytest.approx(0.0, abs=4 * est.standard_error + 1e-12)
    # a trial and its mirror image: the maximum is |Z|, whose mean is sqrt(2 / pi) = 0.797885,
    # more than two independent trials find (0.564190): choosing the sign is a search too
    est = expected_max_normal([[1.0, -1.0], [-1.0, 1.0]], 20_000, rng)
    assert abs(est.value - math.sqrt(2.0 / math.pi)) < 4 * est.standard_error
    # two independent mirror pairs: max(|Z1|, |Z2|), mean int_0^inf 1 - erf(x / sqrt 2)^2 dx
    # = 2 / sqrt(pi) = 1.128379, between 4 (1.029375) and 5 (1.162964) independent trials
    pairs = [
        [1.0, -1.0, 0.0, 0.0],
        [-1.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, -1.0],
        [0.0, 0.0, -1.0, 1.0],
    ]
    est = expected_max_normal(pairs, 20_000, rng)
    assert abs(est.value - 2.0 / math.sqrt(math.pi)) < 4 * est.standard_error
    assert 4.0 < equivalent_trials(2.0 / math.sqrt(math.pi)) < 5.0


def test_the_null_benchmark_scales_by_the_sharpe_ratios_standard_error() -> None:
    # two independent trials over 101 observations: (1 / sqrt(pi)) / sqrt(100) = 0.0564190
    rng = random.Random(2)
    b = null_benchmark([[1.0, 0.0], [0.0, 1.0]], 101, 40_000, rng)
    assert abs(b.value - 0.0564190) < 4 * b.standard_error
    assert b.n == 40_000


def test_invalid_inputs_are_refused() -> None:
    rng = random.Random(0)
    bad_calls = [
        lambda: cholesky([]),
        lambda: cholesky([[1.0, 0.5]]),
        lambda: cholesky([[1.0, 2.0], [2.0, 1.0]]),  # not positive semi-definite
        lambda: cholesky([[1.0, math.nan], [math.nan, 1.0]]),
        lambda: expected_max_normal([[1.0]], 1, rng),
        lambda: null_benchmark([[1.0]], 1, 10, rng),
        lambda: expected_max_iid(0.5),
        lambda: equivalent_trials(math.inf),
        lambda: equivalent_trials(10.0),  # beyond any plausible number of trials
    ]
    for call in bad_calls:
        with pytest.raises(MaximaError):
            call()
