import math
import random
from statistics import variance

import pytest

from finresearch.stats.bootstrap import resample_means
from finresearch.stats.montecarlo import wilson_interval
from finresearch.stats.snooping import (
    SnoopingError,
    reality_check,
    reality_check_p_value,
    resampled_means,
    spa,
    spa_p_values,
    stationary_variance,
    stepdown,
    stepm,
)

CHECK = 0.99


# ---------------------------------------------------------------- the decisions by hand
def test_reality_check_p_value_by_hand() -> None:
    # means 0.25, 0.5, -0.25 over n = 4: V = sqrt(4) x 0.5 = 1. Four resamples, centred maxima
    # 0, 0.5, 0.5, 1.0 -> V* = 0, 1, 1, 2. Three are at or below V: p = 1 - 3/4 = 0.25.
    means = [0.25, 0.5, -0.25]
    draws = [
        [0.25, 0.5, -0.25],
        [0.75, 0.25, -0.25],
        [0.0, 1.0, 0.0],
        [0.25, 0.5, 0.75],
    ]
    r = reality_check_p_value(means, draws, 4)
    assert (r.statistic, r.p_value, r.best) == (1.0, 0.25, 1)


def test_stepdown_by_hand() -> None:
    # means 1, 0.5, 0.25, -0.5; alpha 0.25 over 4 resamples: c is the 3rd smallest maximum.
    # Centred draws (rows are resamples):
    #   [0.75, 0.25, 0.25, 0], [0, 0.375, 0, 0], [1, 0, 0, 0.5], [0.5, 0.25, 0.25, 0]
    # step 1, all four: maxima 0.75, 0.375, 1, 0.5 -> c = 0.75: rejects strategy 0 (1 > 0.75)
    # step 2, {1, 2, 3}: maxima 0.25, 0.375, 0.5, 0.25 -> c = 0.375: rejects 1 (0.5 > 0.375)
    # step 3, {2, 3}: maxima 0.25, 0, 0.5, 0.25 -> c = 0.25: 0.25 is not above it, stop.
    # A single step (what the Reality Check decides) would have found strategy 0 alone.
    means = [1.0, 0.5, 0.25, -0.5]
    centred = [
        [0.75, 0.25, 0.25, 0.0],
        [0.0, 0.375, 0.0, 0.0],
        [1.0, 0.0, 0.0, 0.5],
        [0.5, 0.25, 0.25, 0.0],
    ]
    draws = [[m + c for m, c in zip(means, row, strict=True)] for row in centred]
    r = stepdown(means, draws, 0.25)
    assert r.rejected == (0, 1)
    assert r.steps == ((0,), (1,))
    assert r.critical_values == (0.75, 0.375, 0.25)


def test_the_quantile_is_the_papers() -> None:
    # inf{x : F(x) >= level}: of 1..1000 the 95% quantile is 950, not an interpolation
    means = [0.0]
    draws = [[float(v)] for v in range(1, 1001)]
    r = stepdown(means, draws, 0.05)
    assert r.critical_values == (950.0,)
    assert r.rejected == ()


def test_the_stationary_variance_by_hand() -> None:
    # d = 1, 2, 6: mean 3, deviations -2, -1, 3; gamma_0 = 14/3, gamma_1 = (2 - 3)/3 = -1/3,
    # gamma_2 = -6/3 = -2. Mean block 2 (q = 1/2): kappa(3, 1) = (2/3)(1/2) + (1/3)(1/4) = 5/12,
    # kappa(3, 2) = (1/3)(1/4) + (2/3)(1/2) = 5/12, so omega^2 = 14/3 + 2 (5/12)(-1/3 - 2)
    # = 14/3 - 35/18 = 49/18. Without blocks it is the plain variance gamma_0 = 14/3.
    assert stationary_variance([1.0, 2.0, 6.0], 2.0) == pytest.approx(49.0 / 18.0)
    assert stationary_variance([1.0, 2.0, 6.0], 1.0) == pytest.approx(14.0 / 3.0)


def _kernel_sum(d: list[float], mean_block: float) -> float:
    """Hansen's (2005, p. 372) formula as printed, term by term: O(n^2)."""
    n, q = len(d), 1.0 / mean_block
    m = math.fsum(d) / n
    x = [v - m for v in d]
    gamma = [math.fsum(x[j] * x[j + i] for j in range(n - i)) / n for i in range(n)]
    kappa = [(n - i) / n * (1 - q) ** i + i / n * (1 - q) ** (n - i) for i in range(n)]
    return gamma[0] + 2.0 * math.fsum(kappa[i] * gamma[i] for i in range(1, n))


def test_the_fast_variance_is_the_printed_formula() -> None:
    rng = random.Random(4)
    for n in (2, 3, 37, 200):
        x = [rng.gauss(0.0, 1.0)]
        for _ in range(n - 1):
            x.append(0.5 * x[-1] + rng.gauss(0.0, 1.0))
        for mean_block in (1.0, 1.5, 4.0, 30.0):
            assert stationary_variance(x, mean_block) == pytest.approx(
                _kernel_sum(x, mean_block), rel=1e-12
            )


def test_the_stationary_variance_is_the_bootstraps() -> None:
    # Politis & Romano (1994): omega^2 is exactly the variance of sqrt(n) times the resampled
    # mean, so 40,000 resamples must find it within their own error (about 0.7% here)
    rng = random.Random(3)
    x = [0.0]
    for _ in range(59):
        x.append(0.5 * x[-1] + rng.gauss(0.0, 1.0))
    draws = resample_means(x, 5.0, 40_000, random.Random(9))
    assert len(x) * variance(draws) == pytest.approx(stationary_variance(x, 5.0), rel=0.03)


def test_spa_p_values_by_hand() -> None:
    # n = 16: sqrt(n) = 4 and the threshold is -sqrt(2 log log 16) = -1.428. Means 0.5, -0.1,
    # -1.0 with standard errors 1, 2, 1 give t = 2, -0.2, -4: T = 2, the best is strategy 0.
    # Strategy 1 is mildly poor (-0.2 is above -1.428), strategy 2 clearly poor (-4 is below).
    #   g_l = 0.5, 0, 0      every poor strategy stays where it looks
    #   g_c = 0.5, -0.1, 0   only the clearly poor one stays; strategy 1 moves to 0
    #   g_u = 0.5, -0.1, -1  everything moves to 0 (the Reality Check's choice)
    # Each draw's T* = max(0, max_k 4 (draw_k - g_k) / omega_k):
    #   draw 1: 0.6, -0.1, -1.0 -> 0.4 for all three: none above 2
    #   draw 2: 0.5, 0.0, -0.4 -> l 0, c 0.2, u 2.4 (strategy 2 counted as if it were at 0)
    #   draw 3: 0.5, 1.0, -1.0 -> l 2.0 (not above 2: strictly), c 2.2, u 2.2
    #   draw 4: 1.2, -0.1, -1.0 -> 2.8 for all three
    # p = 1/4, 2/4, 3/4
    means = [0.5, -0.1, -1.0]
    draws = [[0.6, -0.1, -1.0], [0.5, 0.0, -0.4], [0.5, 1.0, -1.0], [1.2, -0.1, -1.0]]
    r = spa_p_values(means, draws, 16, [1.0, 2.0, 1.0])
    assert (r.statistic, r.lower, r.consistent, r.upper, r.best) == (2.0, 0.25, 0.5, 0.75, 0)


def test_spa_bounds_and_the_reality_check() -> None:
    # g_l >= g_c >= g_u everywhere, so the p-values are always ordered (Hansen's theorem 2);
    # with every standard error 1 and a positive best mean, the upper one is the Reality Check
    rng = random.Random(8)
    for _ in range(50):
        means = [rng.gauss(0.0, 0.2) for _ in range(5)]
        draws = [[m + rng.gauss(0.0, 0.25) for m in means] for _ in range(40)]
        omegas = [rng.uniform(0.5, 2.0) for _ in range(5)]
        r = spa_p_values(means, draws, 16, omegas)
        assert r.lower <= r.consistent <= r.upper
        if max(means) > 0.0:
            ones = spa_p_values(means, draws, 16, [1.0] * 5)
            assert ones.upper == pytest.approx(reality_check_p_value(means, draws, 16).p_value)


def test_resampled_means_share_blocks_and_match_the_bootstrap() -> None:
    rng = random.Random(1)
    x = [rng.gauss(0.0, 1.0) for _ in range(50)]
    y = [2.0 * v for v in x]
    means, draws = resampled_means([x, y], 4.0, 30, random.Random(2))
    assert means[1] == pytest.approx(2.0 * means[0])
    # the same blocks for every strategy: y's resampled mean is twice x's, every time
    assert all(row[1] == pytest.approx(2.0 * row[0]) for row in draws)
    # and the same draws as the single-series bootstrap with the same seed
    single = resample_means(x, 4.0, 30, random.Random(2))
    assert [row[0] for row in draws] == pytest.approx(single, abs=1e-12)


# ---------------------------------------------------------------- step 3.4 acceptance
def _family(
    k: int, n: int, edges: dict[int, float], rng: random.Random, rho: float = 0.0
) -> list[list[float]]:
    """``k`` strategies sharing a factor (correlation 0.5), each AR(1) with ``rho``, plus edges."""
    share = math.sqrt(0.5)
    factor = [rng.gauss(0.0, 1.0) for _ in range(n)]
    out = []
    for s in range(k):
        x, series = 0.0, []
        for t in range(n):
            shock = share * factor[t] + math.sqrt(1.0 - share * share) * rng.gauss(0.0, 1.0)
            x = rho * x + shock
            series.append(x + edges.get(s, 0.0))
        out.append(series)
    return out


def _familywise(
    families: int, rho: float, mean_block: float, seed: int
) -> tuple[int, int, int, int]:
    """How many families of 10 no-edge strategies the Reality Check, StepM, SPA (consistent)
    and testing each strategy alone reject at 10% (n = 200, 199 resamples)."""
    alpha, rng = 0.10, random.Random(seed)
    rc = sm = sp = naive = 0
    for _ in range(families):
        performance = _family(10, 200, {}, rng, rho)
        means, draws = resampled_means(performance, mean_block, 199, rng)
        omegas = [math.sqrt(stationary_variance(s, mean_block)) for s in performance]
        rc += reality_check_p_value(means, draws, 200).p_value <= alpha
        sm += bool(stepdown(means, draws, alpha).rejected)
        sp += spa_p_values(means, draws, 200, omegas).consistent <= alpha
        naive += any(
            stepdown([means[k]], [[row[k]] for row in draws], alpha).rejected for k in range(10)
        )
    return rc, sm, sp, naive


def test_pure_noise_families_are_not_found_more_often_than_alpha() -> None:
    # 150 families of 10 strategies with no edge, independent over time, so resampling single
    # observations is valid. The Reality Check, StepM and SPA must not reject more often than
    # 10% (the 99% Wilson interval must reach down to it, decision 0018's rule). Testing each
    # strategy alone at 10% must reject in far more families: the problem these tests solve.
    families = 150
    rc, sm, sp, naive = _familywise(families, 0.0, 1.0, 20261118)
    for hits in (rc, sm, sp):
        assert wilson_interval(hits, families, CHECK)[0] <= 0.10
    assert wilson_interval(naive, families, CHECK)[0] > 0.10


def test_dependence_needs_blocks_and_still_costs_some_size() -> None:
    # The same families, but each strategy AR(1) with 0.3. Single-observation resampling ignores
    # the dependence and rejects far more often than 10% (300 families: 26.7%; here 24%). Blocks
    # of mean length 5 bring that down (300 families: 14.3%, 99% interval 9.9% - 20.3%; here
    # 15.3%) but not to 10%: the block bootstrap understates the variance of a mean by about
    # G / b (step 3.3), here 0.659 / 5 = 13%. The meta-research (3.12) measures this cost for the
    # whole pipeline, and the thresholds are set from what it measures (3.16).
    # SPA's studentizing does not repair this (here 39 and 24 families against the Reality
    # Check's 36 and 23): it divides by the same bootstrap's variance that is too small, so the
    # statistic and its resampled copies are scaled alike.
    families = 150
    _, single, spa_single, _ = _familywise(families, 0.3, 1.0, 11)
    _, blocks, spa_blocks, _ = _familywise(families, 0.3, 5.0, 11)
    assert wilson_interval(single, families, CHECK)[0] > 0.10
    assert wilson_interval(spa_single, families, CHECK)[0] > 0.10
    assert blocks < single
    assert spa_blocks < spa_single


def test_a_real_edge_is_found_and_the_others_left() -> None:
    # One strategy of 10 earns 0.35 a period with standard deviation 1, n = 200: its mean is
    # 0.35 x sqrt(200) = 4.95 standard errors above 0. StepM's first critical value is the 90%
    # quantile of the largest of 10 correlated deviations, below the Bonferroni bound
    # Phi^-1(1 - 0.1 / 10) = 2.33 standard errors, so it is rejected with probability at least
    # Phi(4.95 - 2.33) = 0.996; the nine others should be rejected about as rarely as alpha allows.
    alpha, families = 0.10, 40
    rng = random.Random(7)
    found = false = 0
    for _ in range(families):
        result = stepm(_family(10, 200, {3: 0.35}, rng), alpha, 1.0, 199, rng)
        found += 3 in result.rejected
        false += any(k != 3 for k in result.rejected)
    assert found >= 0.9 * families
    assert wilson_interval(false, families, CHECK)[0] <= alpha
    assert reality_check(_family(10, 200, {3: 0.35}, rng), 1.0, 199, rng).best == 3
    assert spa(_family(10, 200, {3: 0.35}, rng), 1.0, 199, rng).best == 3


def _hansen_design(
    m: int, n: int, poor: float, best: float, rng: random.Random
) -> list[list[float]]:
    """Hansen's (2005) section 4 experiment: losses L_k ~ N(lambda_k / sqrt(n), sigma_k^2) with
    sigma_k^2 = exp(arctan(lambda_k)) / 2, performance d_k = L_0 - L_k. lambda_0 = 0 (the
    benchmark), lambda_1 = best (below 0: better than the benchmark), and the other m - 1 spread
    evenly up to poor (lambda_k = (k - 1) poor / (m - 1)): ever worse, and ever noisier."""
    root = math.sqrt(n)
    lambdas = [best] + [(k - 1) * poor / (m - 1) for k in range(2, m + 1)]
    benchmark = [rng.gauss(0.0, math.sqrt(0.5)) for _ in range(n)]
    return [
        [b - rng.gauss(lam / root, math.sqrt(0.5 * math.exp(math.atan(lam)))) for b in benchmark]
        for lam in lambdas
    ]


def test_spa_keeps_its_power_among_poor_strategies() -> None:
    # Hansen's design with m = 20, n = 200, the other 19 strategies spread down to Lambda_0 = 10
    # (the worst is 10 standard errors below the benchmark), 199 resamples, level 5%. With
    # Lambda_1 = -3 the first strategy's mean is about 3 standard errors above the benchmark.
    # The Reality Check moves all 19 poor ones up to the boundary and, through their noise,
    # raises its critical value: it finds the good one in 5 of 60 samples. SPA keeps them
    # where they are and studentizes: 57 of 60. (Hansen's table 2, m = 100, panel E: .006 and
    # .843.) With Lambda_1 = 0 nothing beats the benchmark and SPA must stay within 5%.
    alpha, samples = 0.05, 60
    rng = random.Random(5)
    rc = found = 0
    for _ in range(samples):
        performance = _hansen_design(20, 200, 10.0, -3.0, rng)
        means, draws = resampled_means(performance, 1.0, 199, rng)
        omegas = [math.sqrt(stationary_variance(s, 1.0)) for s in performance]
        rc += reality_check_p_value(means, draws, 200).p_value <= alpha
        found += spa_p_values(means, draws, 200, omegas).consistent <= alpha
    assert found >= 0.75 * samples
    assert rc <= 0.3 * samples
    false = sum(
        spa(_hansen_design(20, 200, 10.0, 0.0, rng), 1.0, 199, rng).consistent <= alpha
        for _ in range(samples)
    )
    assert wilson_interval(false, samples, CHECK)[0] <= alpha


def test_invalid_inputs_are_refused() -> None:
    rng = random.Random(0)
    bad_calls = [
        lambda: resampled_means([], 2.0, 10, rng),
        lambda: resampled_means([[1.0]], 2.0, 10, rng),
        lambda: resampled_means([[1.0, 2.0], [1.0]], 2.0, 10, rng),
        lambda: resampled_means([[1.0, math.nan]], 2.0, 10, rng),
        lambda: resampled_means([[1.0, 2.0]], 2.0, 0, rng),
        lambda: reality_check_p_value([1.0], [], 4),
        lambda: reality_check_p_value([1.0, 2.0], [[1.0]], 4),
        lambda: stepdown([1.0], [[1.0]], 0.0),
        lambda: stepdown([1.0], [[1.0]], 1.0),
        lambda: stepdown([1.0], [[1.0, 2.0]], 0.1),
        lambda: stationary_variance([1.0, 2.0], 0.5),
        lambda: stationary_variance([1.0, 1.0, 1.0], 2.0),  # no variance, no standard error
        lambda: spa_p_values([1.0], [[1.0]], 2, [1.0]),  # log log 2 is negative
        lambda: spa_p_values([1.0], [[1.0]], 16, [0.0]),
        lambda: spa_p_values([1.0, 2.0], [[1.0, 2.0]], 16, [1.0]),
        lambda: spa_p_values([1.0], [], 16, [1.0]),
    ]
    for call in bad_calls:
        with pytest.raises(SnoopingError):
            call()
