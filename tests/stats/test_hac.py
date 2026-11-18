import math
import random
from statistics import fmean, stdev

import pytest

from finresearch.stats.hac import HacError, long_run_variance, newey_west_t, ordinary_t


def test_newey_west_by_hand() -> None:
    # x = 1, 2, 3, 4: mean 2.5, deviations -1.5, -0.5, 0.5, 1.5
    # g0 = (2.25 + 0.25 + 0.25 + 2.25) / 4 = 1.25; g1 = (0.75 - 0.25 + 0.75) / 4 = 0.3125
    # lag 1: 1.25 + 2 x (1 - 1/2) x 0.3125 = 1.5625; t = 2.5 / sqrt(1.5625 / 4) = 4
    x = [1.0, 2.0, 3.0, 4.0]
    assert long_run_variance(x, 0) == pytest.approx(1.25)
    assert long_run_variance(x, 1) == pytest.approx(1.5625)
    assert newey_west_t(x, 1) == pytest.approx(4.0)
    # ordinary t: s^2 = 5 / 3, t = 2.5 / sqrt(5 / 12) = 3.872983
    assert ordinary_t(x) == pytest.approx(2.5 / math.sqrt(5 / 12))


def test_what_overlap_does_to_each_t() -> None:
    # A 5-day strategy on noise. Each cohort earns the sum of its 5 days' iid shocks; the
    # portfolio's daily return is the average of the 5 live cohorts, an MA(4). Over 1000
    # repetitions of 500 days: the ordinary t of the daily returns has sd sqrt(5) = 2.236;
    # Newey-West with lag 4 recovers 0.68 of the long-run variance, sd sqrt(75 / 51) = 1.213;
    # the cohorts formed every 5 days do not overlap, and their ordinary t has sd 1.
    rng = random.Random(1)
    naive, nw, cohorts = [], [], []
    for _ in range(1000):
        z = [rng.gauss(0.0, 1.0) for _ in range(504)]
        daily = [fmean(z[t - 4 : t + 1]) for t in range(4, 504)]
        naive.append(ordinary_t(daily))
        nw.append(newey_west_t(daily, 4))
        cohorts.append(ordinary_t([math.fsum(z[s : s + 5]) for s in range(0, 500, 5)]))
    assert stdev(naive) == pytest.approx(math.sqrt(5.0), rel=0.06)
    assert stdev(nw) == pytest.approx(math.sqrt(75 / 51), rel=0.06)
    assert stdev(cohorts) == pytest.approx(1.0, rel=0.06)


def test_invalid_inputs_are_refused() -> None:
    bad_calls = [
        lambda: long_run_variance([1.0], 0),
        lambda: long_run_variance([1.0, 2.0], -1),
        lambda: long_run_variance([1.0, 2.0], 2),
        lambda: newey_west_t([1.0, 1.0, 1.0], 1),
        lambda: ordinary_t([2.0, 2.0]),
        lambda: ordinary_t([1.0, math.nan]),
    ]
    for call in bad_calls:
        with pytest.raises(HacError):
            call()
