import math
import random

import pytest

from finresearch.stats.bootstrap import resample_means
from finresearch.stats.montecarlo import wilson_interval
from finresearch.stats.snooping import (
    SnoopingError,
    reality_check,
    reality_check_p_value,
    resampled_means,
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


def _familywise(families: int, rho: float, mean_block: float, seed: int) -> tuple[int, int, int]:
    """How many families of 10 no-edge strategies the Reality Check, StepM and testing each
    strategy alone reject at 10% (n = 200, 199 resamples)."""
    alpha, rng = 0.10, random.Random(seed)
    rc = sm = naive = 0
    for _ in range(families):
        means, draws = resampled_means(_family(10, 200, {}, rng, rho), mean_block, 199, rng)
        rc += reality_check_p_value(means, draws, 200).p_value <= alpha
        sm += bool(stepdown(means, draws, alpha).rejected)
        naive += any(
            stepdown([means[k]], [[row[k]] for row in draws], alpha).rejected for k in range(10)
        )
    return rc, sm, naive


def test_pure_noise_families_are_not_found_more_often_than_alpha() -> None:
    # 150 families of 10 strategies with no edge, independent over time, so resampling single
    # observations is valid. The Reality Check and StepM must not reject more often than 10%
    # (the 99% Wilson interval must reach down to it, decision 0018's rule). Testing each
    # strategy alone at 10% must reject in far more families: the problem these tests solve.
    families = 150
    rc, sm, naive = _familywise(families, 0.0, 1.0, 20261118)
    for hits in (rc, sm):
        assert wilson_interval(hits, families, CHECK)[0] <= 0.10
    assert wilson_interval(naive, families, CHECK)[0] > 0.10


def test_dependence_needs_blocks_and_still_costs_some_size() -> None:
    # The same families, but each strategy AR(1) with 0.3. Single-observation resampling ignores
    # the dependence and rejects far more often than 10% (300 families: 26.7%; here 24%). Blocks
    # of mean length 5 bring that down (300 families: 14.3%, 99% interval 9.9% - 20.3%; here
    # 15.3%) but not to 10%: the block bootstrap understates the variance of a mean by about
    # G / b (step 3.3), here 0.659 / 5 = 13%. The meta-research (3.12) measures this cost for the
    # whole pipeline, and the thresholds are set from what it measures (3.16).
    families = 150
    _, single, _ = _familywise(families, 0.3, 1.0, 11)
    _, blocks, _ = _familywise(families, 0.3, 5.0, 11)
    assert wilson_interval(single, families, CHECK)[0] > 0.10
    assert blocks < single


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
    ]
    for call in bad_calls:
        with pytest.raises(SnoopingError):
            call()
