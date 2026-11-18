import itertools
import math
import random
from statistics import fmean, stdev

import pytest

from finresearch.meta.noise import generate, noise_model
from finresearch.meta.planted import (
    Planted,
    PlantedError,
    estimate_strength,
    plant,
    signal_path,
)
from finresearch.stats.montecarlo import wilson_interval


def test_the_signal_has_unit_variance_and_the_persistence_asked() -> None:
    s = signal_path(40_000, 0.8, random.Random(1))
    m = fmean(s)
    lag1 = math.fsum((a - m) * (b - m) for a, b in itertools.pairwise(s)) / math.fsum(
        (v - m) ** 2 for v in s
    )
    # an AR(1) with 0.8 has the variance of its mean inflated 9 times: sd of the mean 3 / 200
    assert fmean([v * v for v in s]) == pytest.approx(1.0, abs=0.05)
    assert lag1 == pytest.approx(0.8, abs=0.01)


def test_the_effect_is_added_where_it_should_be() -> None:
    noise = [[0.01, -0.02, 0.03, -0.01, 0.02]]
    p = plant(noise, 0.5, 0.0, random.Random(2))
    sd = stdev(noise[0])
    assert p.scale == (pytest.approx(sd),)
    assert p.returns[0][0] == 0.01  # nothing predicts the first period
    for t in range(1, 5):  # each return carries 0.5 sd of the previous period's signal
        assert p.returns[0][t] == pytest.approx(noise[0][t] + 0.5 * sd * p.signal[0][t - 1])


def test_estimate_strength_by_hand() -> None:
    # signal 1, -1, 2 predicts the standardised returns 0.5, -0.5, 1.5 (scale 1):
    # b = (0.5 + 0.5 + 3) / (1 + 1 + 4) = 2/3; residuals -1/6, 1/6, 1/6, sum of squares 1/12;
    # se = sqrt(1/12 / (3 - 1) / 6) = sqrt(1/144) = 1/12
    p = Planted(((0.0, 0.5, -0.5, 1.5),), ((1.0, -1.0, 2.0, 0.0),), 0.0, (1.0,))
    e = estimate_strength(p)
    assert e.value == pytest.approx(2 / 3)
    assert e.standard_error == pytest.approx(1 / 12)
    assert e.n == 3


@pytest.mark.parametrize(("strength", "persistence"), [(0.0, 0.0), (0.05, 0.9)])
def test_the_planted_strength_is_found_with_the_error_theory_gives(
    strength: float, persistence: float
) -> None:
    # 200 repetitions on factory noise, 5 instruments x 400 days: 1995 pairs, so the standard
    # error is about sqrt((1 + strength^2) / 1995) = 0.0224. The estimates must centre on the
    # truth (within 3 standard errors of their mean), spread as the theory says (within 15%),
    # and their 95% intervals must cover the truth about 95% of the time.
    rng = random.Random(20261118)
    m = noise_model()
    estimates, covered = [], 0
    for _ in range(200):
        e = estimate_strength(plant(generate(m, 5, 400, rng), strength, persistence, rng))
        estimates.append(e.value)
        covered += abs(e.value - strength) <= 1.959964 * e.standard_error
    theory = math.sqrt((1 + strength**2) / (5 * 399))
    assert abs(fmean(estimates) - strength) < 3 * theory / math.sqrt(200)
    assert stdev(estimates) == pytest.approx(theory, rel=0.15)
    low, high = wilson_interval(covered, 200, 0.99)
    assert low <= 0.95 <= high


def test_invalid_inputs_are_refused() -> None:
    rng = random.Random(0)
    bad_calls = [
        lambda: signal_path(10, 1.0, rng),
        lambda: plant([], 0.1, 0.0, rng),
        lambda: plant([[0.01, 0.02]], 0.1, 0.0, rng),
        lambda: plant([[0.01, 0.02, 0.03], [0.01, 0.02]], 0.1, 0.0, rng),
        lambda: plant([[0.01, 0.02, 0.03]], math.nan, 0.0, rng),
        lambda: plant([[0.01, 0.01, 0.01]], 0.1, 0.0, rng),
        lambda: plant([[0.01, math.inf, 0.03]], 0.1, 0.0, rng),
    ]
    for call in bad_calls:
        with pytest.raises(PlantedError):
            call()
    assert generate(noise_model(), 1, 3, rng)  # the factory itself is fine with tiny runs
