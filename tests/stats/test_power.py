import math
import random
from statistics import NormalDist

import pytest

from finresearch.stats.montecarlo import wilson_interval
from finresearch.stats.power import (
    PowerError,
    ar1_inflation,
    overlap_inflation,
    required_per_group,
    required_sharpe_observations,
    sharpe_power,
    two_sample_power,
)
from finresearch.stats.sharpe import min_track_record, moments, sharpe_standard_error

CHECK = 0.99  # confidence of the intervals around simulated power
REPLICATIONS = 2000


def test_the_protocol_example_holds() -> None:
    # protocol.md section 2: d = 0.5, t >= 3, power 80%, two groups of similar size: about 240 days
    # per group 2 ((3 + 0.841621) / 0.5)^2 = 2 * 59.032 = 118.06 -> 119; 238 in all
    assert required_per_group(0.5, 3.0, 0.8) == 119
    assert two_sample_power(0.5, 119, 3.0) == pytest.approx(0.8042, abs=1e-4)


def test_sharpe_power_by_hand() -> None:
    # SR 0.1, n 101, normal: se = sqrt(1.005 / 100) = 0.1002497, delta = 0.997509
    # one-sided, threshold 1: Phi(-0.002491) = 0.499006
    assert sharpe_power(0.1, 101, 1.0, two_sided=False) == pytest.approx(0.499006, abs=1e-6)
    # two-sided adds Phi(-0.997509 - 1) = Phi(-1.997509) = 0.022885
    assert sharpe_power(0.1, 101, 1.0) == pytest.approx(0.499006 + 0.022885, abs=1e-6)


def test_required_sharpe_observations_by_hand() -> None:
    # SR 0.1, t 3, power 0.8, normal: 1 + 1.005 * ((3 + 0.841621) / 0.1)^2 = 1484.19 -> 1485
    assert required_sharpe_observations(0.1, 3.0, 0.8) == 1485
    # AR(1) with rho 0.5 triples the long-run variance and so the data needed
    assert required_sharpe_observations(0.1, 3.0, 0.8, inflation=ar1_inflation(0.5)) == 4453


def test_a_huge_effect_still_needs_two_observations() -> None:
    # SR 5, t 1, power 0.6: 1 + 13.5 * ((1 + 0.253347) / 5)^2 = 1.848 -> at least 2
    assert required_sharpe_observations(5.0, 1.0, 0.6) == 2
    assert required_sharpe_observations(5.0, 1.0, 0.6, inflation=3.0) == 6
    # d 10, t 1, power 0.6: 2 ((1 + 0.253347) / 10)^2 = 0.031 -> at least 2 per group
    assert required_per_group(10.0, 1.0, 0.6) == 2
    assert two_sample_power(10.0, 2, 1.0, two_sided=False) >= 0.6


def test_half_power_is_the_minimum_track_record() -> None:
    # power 0.5 puts the threshold at the expected statistic: the minimum track record length
    trl = min_track_record(0.1, 0.0, -1.0, 6.0, NormalDist().cdf(3.0))
    assert required_sharpe_observations(0.1, 3.0, 0.5, skew=-1.0, kurt=6.0) == math.ceil(trl)


def test_inflation_by_hand() -> None:
    assert ar1_inflation(0.0) == 1.0
    assert ar1_inflation(0.5) == pytest.approx(3.0)
    assert ar1_inflation(-0.5) == pytest.approx(1 / 3)
    assert overlap_inflation(5) == 5.0


def test_power_moves_the_right_way() -> None:
    base = sharpe_power(0.1, 1000, 3.0)
    assert sharpe_power(0.1, 2000, 3.0) > base  # more data
    assert sharpe_power(0.15, 1000, 3.0) > base  # bigger effect
    assert sharpe_power(0.1, 1000, 2.0) > base  # lower bar
    assert sharpe_power(0.1, 1000, 3.0, inflation=2.0) < base  # dependence
    assert sharpe_power(0.1, 1000, 3.0, skew=-1.0, kurt=8.0) < base  # fat left tail
    assert sharpe_power(0.0, 1000, 3.0) == pytest.approx(2 * (1 - NormalDist().cdf(3.0)))


def test_invalid_inputs_are_refused() -> None:
    bad_calls = [
        lambda: sharpe_power(0.1, 100, 0.0),
        lambda: sharpe_power(0.1, 100, 3.0, inflation=0.0),
        lambda: sharpe_power(0.1, 3, 3.0, inflation=2.0),
        lambda: required_sharpe_observations(0.0, 3.0, 0.8),
        lambda: required_sharpe_observations(0.1, 3.0, 1.0),
        lambda: required_sharpe_observations(0.1, 3.0, 0.8, inflation=-1.0),
        lambda: two_sample_power(0.5, 1, 3.0),
        lambda: required_per_group(0.0, 3.0, 0.8),
        lambda: ar1_inflation(1.0),
        lambda: overlap_inflation(0),
    ]
    for call in bad_calls:
        with pytest.raises(PowerError):
            call()


# ---------------------------------------------------------------- against simulation
def _agrees(hits: int, expected: float) -> bool:
    low, high = wilson_interval(hits, REPLICATIONS, CHECK)
    return low <= expected <= high


def test_sharpe_power_matches_simulation() -> None:
    # n 400 normal returns with SR 0.15, threshold 3: power about 0.49
    sr, n, threshold = 0.15, 400, 3.0
    rnd = random.Random(1)
    hits = 0
    for _ in range(REPLICATIONS):
        m = moments([sr + rnd.gauss() for _ in range(n)])
        hits += abs(m.sharpe / sharpe_standard_error(m.sharpe, n, m.skew, m.kurt)) >= threshold
    assert _agrees(hits, sharpe_power(sr, n, threshold))


def test_dependence_matches_simulation() -> None:
    # AR(1) returns, rho 0.5, SR 0.15 against the stationary deviation, n 800, threshold 2.
    # The statistic is the one the formula describes: the estimated Sharpe ratio over its
    # standard error, computed with the effective count n / inflation (as an analyst who knows
    # or has estimated the dependence would).
    sr, n, rho, threshold = 0.15, 800, 0.5, 2.0
    inflation = ar1_inflation(rho)
    n_eff = n / inflation
    innovation = math.sqrt(1.0 - rho * rho)  # stationary variance 1
    rnd = random.Random(99)
    hits = 0
    for _ in range(REPLICATIONS):
        x, returns = rnd.gauss(), []
        for _ in range(n):
            x = rho * x + innovation * rnd.gauss()
            returns.append(sr + x)
        m = moments(returns)
        spread = sharpe_standard_error(m.sharpe, 2, m.skew, m.kurt) ** 2  # (n - 1) se^2
        hits += abs(m.sharpe / math.sqrt(spread / (n_eff - 1.0))) >= threshold
    assert _agrees(hits, sharpe_power(sr, n, threshold, inflation=inflation))
    # ignoring the dependence would promise far more power than there is (0.99 instead of 0.67)
    assert not _agrees(hits, sharpe_power(sr, n, threshold))


def test_two_sample_power_matches_simulation() -> None:
    d, n, threshold = 0.5, 119, 3.0
    rnd = random.Random(3)
    hits = 0
    for _ in range(REPLICATIONS):
        a = [d + rnd.gauss() for _ in range(n)]
        b = [rnd.gauss() for _ in range(n)]
        ma, mb = math.fsum(a) / n, math.fsum(b) / n
        va = math.fsum((x - ma) ** 2 for x in a) / (n - 1)
        vb = math.fsum((x - mb) ** 2 for x in b) / (n - 1)
        hits += abs((ma - mb) / math.sqrt(va / n + vb / n)) >= threshold
    assert _agrees(hits, two_sample_power(d, n, threshold))
