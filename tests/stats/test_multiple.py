import math
import random
from statistics import NormalDist

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finresearch.stats.montecarlo import mean, wilson_interval
from finresearch.stats.multiple import (
    Method,
    MultipleTestingError,
    adjust,
    benjamini_hochberg,
    benjamini_yekutieli,
    harmonic,
    holm,
    reject,
)

# Sorted: 0.005 (#3), 0.01 (#0), 0.03 (#2), 0.04 (#1); m = 4
P = [0.01, 0.04, 0.03, 0.005]


def test_holm_by_hand() -> None:
    # (m - j + 1) p_(j): 4 * 0.005 = 0.02, 3 * 0.01 = 0.03, 2 * 0.03 = 0.06, 1 * 0.04 = 0.04
    # running max: 0.02, 0.03, 0.06, 0.06
    assert holm(P) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert reject(P, 0.05, Method.HOLM) == [True, False, False, True]


def test_benjamini_hochberg_by_hand() -> None:
    # m p_(j) / j: 0.02, 0.02, 0.04, 0.04; running min from the top: 0.02, 0.02, 0.04, 0.04
    assert benjamini_hochberg(P) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert reject(P, 0.05, Method.BH) == [True, True, True, True]


def test_benjamini_yekutieli_by_hand() -> None:
    # c(4) = 1 + 1/2 + 1/3 + 1/4 = 25/12
    # BH terms times 25/12: 0.041667, 0.041667, 0.083333, 0.083333 (already non-decreasing)
    assert harmonic(4) == pytest.approx(25 / 12)
    expected = [0.041667, 0.083333, 0.083333, 0.041667]
    assert benjamini_yekutieli(P) == pytest.approx(expected, abs=1e-6)
    assert reject(P, 0.05, Method.BY) == [True, False, False, True]


def test_adjusted_values_are_capped_at_one() -> None:
    assert holm([0.5, 0.6, 0.7]) == [1.0, 1.0, 1.0]
    assert benjamini_hochberg([0.9, 0.95]) == [0.95, 0.95]
    assert benjamini_yekutieli([0.9, 0.95]) == [1.0, 1.0]


def test_edge_cases() -> None:
    for method in Method:
        assert adjust([], method) == []
        assert adjust([0.03], method) == [0.03]
    assert harmonic(0) == 0.0
    assert holm([0.02, 0.02]) == [0.04, 0.04]  # ties get the same value


@pytest.mark.parametrize("bad", [[-0.1], [1.1], [math.nan], [0.1, math.inf]])
def test_invalid_p_values_are_refused(bad: list[float]) -> None:
    for method in Method:
        with pytest.raises(MultipleTestingError):
            adjust(bad, method)


def test_invalid_levels_are_refused() -> None:
    for alpha in (0.0, 1.0, -0.5):
        with pytest.raises(MultipleTestingError):
            reject(P, alpha, Method.HOLM)
    with pytest.raises(MultipleTestingError):
        harmonic(-1)


p_lists = st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=30)


@given(p_lists, st.randoms(use_true_random=False))
def test_order_of_the_input_does_not_matter(p: list[float], rnd: random.Random) -> None:
    order = list(range(len(p)))
    rnd.shuffle(order)
    for method in Method:
        adjusted = adjust(p, method)
        assert adjust([p[i] for i in order], method) == [adjusted[i] for i in order]


@given(p_lists)
def test_adjusted_values_are_valid_and_monotone(p: list[float]) -> None:
    by_rank = sorted(range(len(p)), key=lambda i: p[i])
    for method in Method:
        adjusted = adjust(p, method)
        assert all(raw <= a <= 1.0 for raw, a in zip(p, adjusted, strict=True))
        ranked = [adjusted[i] for i in by_rank]
        assert ranked == sorted(ranked)


@given(p_lists)
def test_benjamini_hochberg_rejects_at_least_what_the_others_reject(p: list[float]) -> None:
    # Holm: (m - j + 1) >= m / j because (j - 1)(m - j) >= 0; BY multiplies BH by c(m) >= 1
    bh = benjamini_hochberg(p)
    assert all(h >= b for h, b in zip(holm(p), bh, strict=True))
    assert all(y >= b for y, b in zip(benjamini_yekutieli(p), bh, strict=True))


# ---------------------------------------------------------------- error rates on known families
ALPHA = 0.05
M, M0 = 20, 15  # 20 tests, the first 15 true nulls
SHIFT = 3.0  # the 5 real effects: z ~ N(3, 1)
FAMILIES = 4000
CHECK = 0.99  # confidence of the intervals that compare a measured rate with its level
NORMAL = NormalDist()


def _family(rnd: random.Random, rho: float) -> list[float]:
    """One-sided p-values; nulls share a factor with correlation ``rho`` (0: independent)."""
    common = rnd.gauss()
    z = [math.sqrt(rho) * common + math.sqrt(1.0 - rho) * rnd.gauss() for _ in range(M)]
    z = [v + (SHIFT if i >= M0 else 0.0) for i, v in enumerate(z)]
    return [1.0 - NORMAL.cdf(v) for v in z]


def _simulate(method: Method | None, rho: float, seed: int) -> tuple[int, list[float], float]:
    """Families with any false rejection, false discovery proportions, mean discoveries."""
    rnd = random.Random(seed)
    any_false, fdp, found = 0, [], 0
    for _ in range(FAMILIES):
        p = _family(rnd, rho)
        rejected = [x <= ALPHA for x in p] if method is None else reject(p, ALPHA, method)
        false = sum(rejected[:M0])
        total = sum(rejected)
        any_false += false > 0
        fdp.append(false / total if total else 0.0)
        found += total - false
    return any_false, fdp, found / FAMILIES


def test_holm_keeps_the_family_wise_error_rate() -> None:
    hits, _, _ = _simulate(Method.HOLM, 0.0, seed=1)
    low, _ = wilson_interval(hits, FAMILIES, CHECK)
    assert low <= ALPHA
    hits, _, _ = _simulate(Method.HOLM, 0.5, seed=2)
    low, _ = wilson_interval(hits, FAMILIES, CHECK)
    assert low <= ALPHA


def test_the_check_catches_an_uncorrected_family() -> None:
    # Without correction, P(any of 15 nulls below 0.05) = 1 - 0.95^15 = 0.54: far above the level
    hits, _, _ = _simulate(None, 0.0, seed=3)
    low, _ = wilson_interval(hits, FAMILIES, CHECK)
    assert low > ALPHA


def test_benjamini_hochberg_keeps_the_false_discovery_rate() -> None:
    # Independent: FDR = alpha m0 / m = 0.0375.
    # Positively correlated one-sided tests (PRDS, Benjamini & Yekutieli 2001): at most that.
    bound = ALPHA * M0 / M
    z = NORMAL.inv_cdf(CHECK)
    for rho, seed in ((0.0, 4), (0.5, 5)):
        _, fdp, _ = _simulate(Method.BH, rho, seed)
        est = mean(fdp)
        assert est.value - z * est.standard_error <= bound


def test_benjamini_yekutieli_keeps_the_false_discovery_rate() -> None:
    z = NORMAL.inv_cdf(CHECK)
    for rho, seed in ((0.0, 6), (0.5, 7)):
        _, fdp, _ = _simulate(Method.BY, rho, seed)
        est = mean(fdp)
        assert est.value - z * est.standard_error <= ALPHA * M0 / M


def test_benjamini_hochberg_finds_more_than_the_stricter_methods() -> None:
    found = {m: _simulate(m, 0.0, seed=8)[2] for m in Method}
    assert found[Method.BH] > found[Method.HOLM]
    assert found[Method.BH] > found[Method.BY]
    assert found[Method.HOLM] > 0.0
