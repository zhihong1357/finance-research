import math
from statistics import NormalDist

import pytest

from finresearch.stats.montecarlo import (
    Estimate,
    EstimateError,
    detection_power,
    mean,
    proportion,
    runs_for_relative_error,
    runs_to_detect,
    wilson_interval,
)

T_GATE = 1.0 - NormalDist().cdf(3.0)  # 0.134990%: t >= 3 in the predicted direction


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


def test_the_ends_of_the_interval_are_exact() -> None:
    # 0 hits bound the rate below by exactly 0, n hits above by exactly 1, at any n and confidence
    for n in (1, 7, 30, 99_991):
        for confidence in (0.5, 0.9, 0.99):
            assert wilson_interval(0, n, confidence)[0] == 0.0
            assert wilson_interval(n, n, confidence)[1] == 1.0


def test_mean_by_hand() -> None:
    # mean 2.5; sample variance (2.25 + 0.25 + 0.25 + 2.25) / 3 = 5 / 3
    # se = sqrt(5 / 3 / 4) = sqrt(5 / 12) = 0.645497
    est = mean([1.0, 2.0, 3.0, 4.0])
    assert (est.value, est.n) == (2.5, 4)
    assert est.standard_error == pytest.approx(math.sqrt(5 / 12))
    assert mean(iter([7.0, 7.0, 7.0])) == Estimate(7.0, 0.0, 3)
    assert mean([0.0, 0.0]) == Estimate(0.0, 0.0, 2)


def test_huge_values_do_not_overflow() -> None:
    # mean 1e308 / 3; deviations 2/3, 2/3, -4/3 (x 1e308): variance (4/9 + 4/9 + 16/9) / 2 = 4/3
    # se = sqrt(4 / 3 / 3) = 2 / 3 (x 1e308)
    est = mean([1e308, 1e308, -1e308])
    assert est.value == pytest.approx(1e308 / 3)
    assert est.standard_error == pytest.approx(2 / 3 * 1e308)


def test_runs_for_relative_error_by_hand() -> None:
    # p = 0.0027 (t >= 3, two-sided): (1 - p) / (p r^2)
    # r = 0.1: 0.9973 / 0.000027 = 36937.04 -> 36938
    # r = 0.06: 0.9973 / 0.00000972 = 102602.9 -> 102603
    assert runs_for_relative_error(0.0027, 0.1) == 36938
    assert runs_for_relative_error(0.0027, 0.06) == 102603
    assert runs_for_relative_error(0.5, 0.1) == 100


def test_detection_power_by_hand() -> None:
    # nominal 10%, true 30%, 10 runs, 90% interval (z = 1.644854, z^2 = 2.705543, scale 1.270554):
    # 2 hits: center (0.2 + 0.135277) / 1.270554 = 0.263882,
    #   half 1.294598 * sqrt(0.016 + 0.006764) = 0.195326, lower 0.068556 <= 0.1
    # 3 hits: center 0.342589, half 1.294598 * sqrt(0.021 + 0.006764) = 0.215713, lower 0.126876
    # so 3 hits are needed: P(X >= 3) = 1 - (0.7^10 + 10 0.3 0.7^9 + 45 0.09 0.7^8)
    # = 1 - (0.028248 + 0.121061 + 0.233474) = 0.617217
    assert detection_power(0.1, 0.3, 10, 0.90) == pytest.approx(0.617217, abs=1e-6)
    # no number of hits in 2 runs gets the lower bound above 99%: no chance at all
    assert detection_power(0.99, 0.995, 2, 0.90) == 0.0


def test_detection_power_agrees_with_counting_every_outcome() -> None:
    # brute force: add up P(X = k) over every k whose Wilson lower bound is above the nominal
    for n in (1, 3, 12, 40, 150):
        for nominal, true_rate in [(0.01, 0.05), (0.1, 0.3), (0.4, 0.6), (0.05, 0.075)]:
            brute = math.fsum(
                math.comb(n, k) * true_rate**k * (1.0 - true_rate) ** (n - k)
                for k in range(n + 1)
                if wilson_interval(k, n, 0.95)[0] > nominal
            )
            assert detection_power(nominal, true_rate, n, 0.95) == pytest.approx(brute, abs=1e-12)


def test_the_run_counts_of_decision_0023() -> None:
    # 80% power to catch a rate 1.5 times the nominal level at 99% (two-sided Wilson, as judged)
    assert runs_to_detect(T_GATE, 1.5 * T_GATE, 0.8, 0.99) == 40_062
    assert runs_to_detect(0.05, 0.075, 0.8, 0.99) == 1_008
    # the exact power is a sawtooth: just below the count it falls short
    assert detection_power(T_GATE, 1.5 * T_GATE, 40_061, 0.99) < 0.8
    assert detection_power(T_GATE, 1.5 * T_GATE, 40_062, 0.99) >= 0.8
    assert detection_power(0.05, 0.075, 1_007, 0.99) < 0.8
    # the normal approximation's counts (38,485 and 982) fall short of 80% for the t gate,
    # and the one-sided counts decision 0018 first gave (33,344 and 850) by far
    assert detection_power(T_GATE, 1.5 * T_GATE, 38_485, 0.99) < 0.8
    assert detection_power(T_GATE, 1.5 * T_GATE, 33_344, 0.99) < 0.73
    assert detection_power(0.05, 0.075, 850, 0.99) < 0.75


def test_beyond_the_count_the_power_holds() -> None:
    for n in range(1_008, 2_016):
        assert detection_power(0.05, 0.075, n, 0.99) >= 0.8


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
    for nominal, true_rate in [(0.0, 0.1), (0.1, 0.1), (0.2, 0.1), (0.1, 1.0)]:
        with pytest.raises(EstimateError):
            detection_power(nominal, true_rate, 100, 0.99)
        with pytest.raises(EstimateError):
            runs_to_detect(nominal, true_rate, 0.8, 0.99)
    with pytest.raises(EstimateError):
        detection_power(0.1, 0.2, 0, 0.99)
    for power in (0.0, 1.0):
        with pytest.raises(EstimateError):
            runs_to_detect(0.1, 0.2, power, 0.99)
