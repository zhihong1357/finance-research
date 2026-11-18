import math

import pytest

from finresearch.stats.montecarlo import (
    Estimate,
    EstimateError,
    mean,
    proportion,
    runs_for_relative_error,
    wilson_interval,
)


def test_proportion_by_hand() -> None:
    # p = 3 / 1000 = 0.003; se = sqrt(0.003 * 0.997 / 1000) = sqrt(2.991e-6) = 0.00172945
    est = proportion(3, 1000)
    assert (est.value, est.n) == (0.003, 1000)
    assert est.standard_error == pytest.approx(0.00172945, abs=1e-8)
    assert proportion(0, 10) == Estimate(0.0, 0.0, 10)
    assert proportion(10, 10) == Estimate(1.0, 0.0, 10)


def test_wilson_interval_with_no_hits_by_hand() -> None:
    # 0 hits: the interval is [0, z^2 / (n + z^2)]; z = 1.959964, z^2 = 3.841459
    # n = 100: 3.841459 / 103.841459 = 0.036994
    low, high = wilson_interval(0, 100, 0.95)
    assert low == 0.0
    assert high == pytest.approx(0.036994, abs=1e-6)
    assert high == pytest.approx(3.841459 / 103.841459, abs=1e-6)


def test_wilson_interval_at_one_half_by_hand() -> None:
    # 5 of 10: p = 0.5, scale = 1 + 3.841459 / 10 = 1.3841459
    # center = (0.5 + 0.192073) / scale = 0.5
    # half = 1.959964 / 1.3841459 * sqrt(0.025 + 3.841459 / 400) = 1.416010 * 0.186020 = 0.263407
    low, high = wilson_interval(5, 10, 0.95)
    assert (low, high) == (pytest.approx(0.236593, abs=1e-6), pytest.approx(0.763407, abs=1e-6))


def test_wilson_interval_contains_the_share_and_narrows_with_n() -> None:
    for hits, n in [(1, 7), (27, 10_000), (999, 1000), (1000, 1000)]:
        low, high = wilson_interval(hits, n, 0.95)
        assert 0.0 <= low <= hits / n <= high <= 1.0
    wide = wilson_interval(27, 10_000, 0.95)
    narrow = wilson_interval(270, 100_000, 0.95)
    assert narrow[1] - narrow[0] < wide[1] - wide[0]
    assert wilson_interval(27, 10_000, 0.5)[1] < wide[1]


def test_mean_by_hand() -> None:
    # mean 2.5; sample variance (2.25 + 0.25 + 0.25 + 2.25) / 3 = 5 / 3
    # se = sqrt(5 / 3 / 4) = sqrt(5 / 12) = 0.645497
    est = mean([1.0, 2.0, 3.0, 4.0])
    assert (est.value, est.n) == (2.5, 4)
    assert est.standard_error == pytest.approx(math.sqrt(5 / 12))
    assert mean(iter([7.0, 7.0, 7.0])) == Estimate(7.0, 0.0, 3)


def test_runs_for_relative_error_by_hand() -> None:
    # p = 0.0027 (t >= 3, two-sided): (1 - p) / (p r^2)
    # r = 0.1: 0.9973 / 0.000027 = 36937.04 -> 36938
    # r = 0.06: 0.9973 / 0.00000972 = 102602.9 -> 102603
    assert runs_for_relative_error(0.0027, 0.1) == 36938
    assert runs_for_relative_error(0.0027, 0.06) == 102603
    assert runs_for_relative_error(0.5, 0.1) == 100


@pytest.mark.parametrize(("hits", "n"), [(1, 0), (-1, 10), (11, 10)])
def test_bad_counts_are_refused(hits: int, n: int) -> None:
    with pytest.raises(EstimateError):
        proportion(hits, n)
    with pytest.raises(EstimateError):
        wilson_interval(hits, n, 0.95)


def test_bad_arguments_are_refused() -> None:
    for confidence in (0.0, 1.0, 1.5):
        with pytest.raises(EstimateError):
            wilson_interval(1, 10, confidence)
    with pytest.raises(EstimateError):
        mean([1.0])
    with pytest.raises(EstimateError):
        mean([1.0, math.nan])
    for p, r in [(0.0, 0.1), (1.0, 0.1), (0.1, 0.0)]:
        with pytest.raises(EstimateError):
            runs_for_relative_error(p, r)
