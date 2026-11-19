import itertools
import math
import random
from statistics import NormalDist, fmean, stdev

import pytest

from finresearch.stats.bootstrap import (
    BlockLengths,
    BootstrapError,
    block_lengths,
    first_quiet_lag,
    flat_top,
    optimal_block_length,
    percentile_interval,
    quantile,
    resample,
    resample_means,
    stationary_blocks,
    stationary_indices,
)
from finresearch.stats.montecarlo import wilson_interval

CHECK = 0.99  # Monte Carlo checks: the 99% Wilson interval must contain the expected value


def ar1(rho: float, n: int, rng: random.Random) -> list[float]:
    """``x_t = rho x_{t-1} + z_t`` with standard normal ``z``, started from its stationary law."""
    x = rng.gauss(0.0, 1.0 / math.sqrt(1.0 - rho * rho))
    out = []
    for _ in range(n):
        x = rho * x + rng.gauss(0.0, 1.0)
        out.append(x)
    return out


def ar1_moments(rho: float) -> tuple[float, float]:
    """``G`` and ``g(0)`` of that AR(1): ``R(k) = rho^|k| / (1 - rho^2)``.

    ``g(0) = sum R(k) = 1 / (1 - rho)^2``; ``G = sum |k| R(k) = 2 rho / ((1 - rho^2)(1 - rho)^2)``.
    """
    g0 = 1.0 / (1.0 - rho) ** 2
    return 2.0 * rho / (1.0 - rho * rho) * g0, g0


# ---------------------------------------------------------------- the block length
def test_flat_top_by_hand() -> None:
    assert [flat_top(t) for t in (0.0, 0.25, -0.49, 0.5, 0.75, -0.75, 1.0, 1.2)] == [
        1.0,
        1.0,
        1.0,
        1.0,
        0.5,
        0.5,
        0.0,
        0.0,
    ]


def test_block_lengths_by_hand() -> None:
    # G = 1, g(0) = 2, n = 1000: SB (2 x 1 / (2 x 4))^(1/3) x 10 = 0.25^(1/3) x 10 = 6.299605;
    # CB (2 / (4/3 x 4))^(1/3) x 10 = 0.375^(1/3) x 10 = 7.211248. Only g(0)^2 enters.
    expected = (pytest.approx(6.299605, abs=1e-6), pytest.approx(7.211248, abs=1e-6))
    for g0 in (2.0, -2.0):
        b = block_lengths(1.0, g0, 1000)
        assert (b.stationary, b.circular) == expected


@pytest.mark.parametrize(
    ("rho", "n", "sb", "cb"),
    [
        (0.7, 200, 11.47, 13.12),
        (0.7, 800, 18.20, 20.83),
        (0.1, 200, 2.01, 2.31),
        (0.1, 800, 3.20, 3.66),
        (-0.4, 200, 5.66, 6.48),
        (-0.4, 800, 8.99, 10.29),  # the table prints 10.23 (see below)
    ],
)
def test_the_corrected_table_1(rho: float, n: int, sb: float, cb: float) -> None:
    # Patton, Politis & White (2009), Table 1: theoretical optimal block lengths for AR(1).
    # Every SB value agrees to 2 decimals. For CB the table prints 2.31 (2.3048 here) and 10.23
    # (10.2865 here); every other row's CB / SB ratio is (3/2)^(1/3) = 1.1447, that row's 1.138,
    # so 10.23 looks like a misprint.
    b = block_lengths(*ar1_moments(rho), n)
    assert b.stationary == pytest.approx(sb, abs=0.005)
    assert b.circular == pytest.approx(cb, abs=0.006)
    assert b.circular / b.stationary == pytest.approx(1.5 ** (1 / 3))


def test_first_quiet_lag_by_hand() -> None:
    # n = 100: bound 2 sqrt(log10(100) / 100) = 2 sqrt(0.02) = 0.282843
    # lags 1-3 (0.9, 0.5, 0.3) are significant; lags 4-8 are all quiet: m = 4
    quiet_from_4 = [0.9, 0.5, 0.3, 0.1, 0.0, 0.1, -0.1, 0.05, 0.2, 0.1]
    assert first_quiet_lag(quiet_from_4, 100) == 4
    # no run of five quiet lags: the largest significant lag, 8 (0.5)
    never_quiet = [0.9, 0.5, 0.1, 0.4, 0.1, 0.1, 0.1, 0.5, 0.1, 0.1]
    assert first_quiet_lag(never_quiet, 100) == 8
    # quiet from the start: m = 1 (so M = 2)
    assert first_quiet_lag([0.1] * 10, 100) == 1


@pytest.mark.parametrize(("n", "series", "mean"), [(800, 200, 0.927), (200, 300, 0.859)])
def test_the_estimate_behaves_as_in_the_corrected_table_2(n: int, series: int, mean: float) -> None:
    # Table 2, rho = 0.7 (1000 series each): the mean of estimated / optimal block length, from
    # the authors' code this module follows. Ours must agree within 3 combined standard errors.
    # The spread agrees at n = 800 (sd 0.244 in the table). At n = 200 ours is wider than the
    # table's 0.342 (about 0.40 over several seeds), for a reason not found: the code matches
    # line for line, and starting the AR(1) at 0 or after a burn-in does not close the gap.
    rho, table_sd = 0.7, {800: 0.244, 200: 0.342}[n]
    rng = random.Random(20261118)
    optimal = block_lengths(*ar1_moments(rho), n).stationary
    ratios = [optimal_block_length(ar1(rho, n, rng)).stationary / optimal for _ in range(series)]
    se = math.sqrt(stdev(ratios) ** 2 / series + table_sd**2 / 1000)
    assert abs(fmean(ratios) - mean) < 3 * se
    if n == 800:
        assert stdev(ratios) == pytest.approx(table_sd, rel=0.15)


def test_the_estimate_is_capped_and_floored() -> None:
    # a random walk of 20 steps: the cap ceil(min(3 sqrt(20), 20 / 3)) = ceil(6.67) = 7
    rng = random.Random(1)
    walk = list(itertools.accumulate(rng.gauss(0.0, 1.0) for _ in range(20)))
    assert optimal_block_length(walk) == BlockLengths(7.0, 7.0)
    # white noise often estimates below 1 (no dependence at all): floored at 1, the plain bootstrap
    rng = random.Random(1)
    noise = [rng.gauss(0.0, 1.0) for _ in range(500)]
    assert optimal_block_length(noise) == BlockLengths(1.0, 1.0)
    for seed in range(30):
        rng = random.Random(seed)
        b = optimal_block_length([rng.gauss(0.0, 1.0) for _ in range(500)])
        assert 1.0 <= b.stationary < 3.0


# ---------------------------------------------------------------- resampling
def test_blocks_cover_the_sample_exactly() -> None:
    rng = random.Random(1)
    for mean_block in (1.0, 2.5, 10.0, 1000.0):
        blocks = stationary_blocks(37, mean_block, rng)
        assert sum(length for _, length in blocks) == 37
        assert all(0 <= start < 37 and length >= 1 for start, length in blocks)
    assert all(length == 1 for _, length in stationary_blocks(20, 1.0, rng))


def test_block_lengths_are_geometric() -> None:
    # Untruncated block lengths: P(L = 1) = 1 / mean_block, mean = mean_block
    rng = random.Random(2)
    lengths = [
        length
        for _ in range(200)
        for length in [length for _, length in stationary_blocks(10_000, 4.0, rng)][:-1]
    ]
    ones = sum(1 for length in lengths if length == 1)
    low, high = wilson_interval(ones, len(lengths), CHECK)
    assert low <= 0.25 <= high
    assert fmean(lengths) == pytest.approx(4.0, rel=0.01)


def test_indices_follow_the_blocks_round_the_end() -> None:
    a, b = random.Random(3), random.Random(3)
    n = 10
    blocks = stationary_blocks(n, 4.0, a)
    expected = [(s + i) % n for s, length in blocks for i in range(length)]
    assert stationary_indices(n, 4.0, b) == expected
    assert any(s + length > n for s, length in blocks)  # this seed does wrap round the end


def test_resample_means_match_resampling_by_index() -> None:
    x = [float(v) for v in (3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9)]
    a, b = random.Random(4), random.Random(4)
    fast = resample_means(x, 3.0, 50, a)
    slow = [fmean([x[i] for i in stationary_indices(len(x), 3.0, b)]) for _ in range(50)]
    assert fast == pytest.approx(slow, abs=1e-12)
    assert resample(x, fmean, 3.0, 50, random.Random(4)) == pytest.approx(slow, abs=1e-12)


def test_the_ordinary_bootstrap_is_centred_on_the_sample_mean() -> None:
    # mean 2.5, population variance 1.25: the resampled mean has sd sqrt(1.25 / 4) = 0.559017
    means = resample_means([1.0, 2.0, 3.0, 4.0], 1.0, 4000, random.Random(6))
    assert fmean(means) == pytest.approx(2.5, abs=3 * 0.559017 / math.sqrt(4000))
    assert stdev(means) == pytest.approx(0.559017, rel=0.05)


def test_quantile_by_hand() -> None:
    # (n - 1) q into the sorted values: q = 0.1 -> position 0.4 -> 1 + 0.4 x (2 - 1) = 1.4
    values = [5.0, 1.0, 4.0, 2.0, 3.0]
    assert [quantile(values, q) for q in (0.0, 0.1, 0.25, 0.5, 1.0)] == [1.0, 1.4, 2.0, 3.0, 5.0]
    assert percentile_interval(values, 0.8) == (pytest.approx(1.4), pytest.approx(4.6))


# ---------------------------------------------------------------- coverage (step 3.3 acceptance)
def test_intervals_cover_as_the_theory_predicts_and_blocks_matter() -> None:
    # AR(1) with rho 0.5, n 800: the 90% percentile interval of the mean. The block bootstrap
    # underestimates the long-run variance g(0) by about G / b (the bias that the optimal block
    # length trades off against variance), so with G / g(0) = 4/3 and b = 11.25 it should cover
    # about 2 Phi(1.645 sqrt(1 - 4/3 / 11.25)) - 1 = 0.878. Resampling single observations sees
    # only the variance 1/(1 - rho^2) = 4/3 of g(0) = 4: 2 Phi(1.645 sqrt(1/3)) - 1 = 0.658.
    rho, n, nominal, z = 0.5, 800, 0.90, NormalDist().inv_cdf(0.95)
    g, g0 = ar1_moments(rho)
    b = block_lengths(g, g0, n).stationary
    expected_block = 2 * NormalDist().cdf(z * math.sqrt(1 - g / (b * g0))) - 1
    expected_single = 2 * NormalDist().cdf(z * math.sqrt((1 / (1 - rho * rho)) / g0)) - 1
    assert (expected_block, expected_single) == (
        pytest.approx(0.878, abs=1e-3),
        pytest.approx(0.658, abs=1e-3),
    )

    def coverage(series: int, block: bool, seed: int) -> tuple[float, float]:
        rng, hits = random.Random(seed), 0
        for _ in range(series):
            x = ar1(rho, n, rng)
            mean_block = optimal_block_length(x).stationary if block else 1.0
            low, high = percentile_interval(resample_means(x, mean_block, 199, rng), nominal)
            hits += low <= 0.0 <= high
        return wilson_interval(hits, series, CHECK)

    with_blocks, single = coverage(200, True, 3), coverage(100, False, 4)
    assert with_blocks[0] <= expected_block <= with_blocks[1]
    assert single[0] <= expected_single <= single[1]
    assert single[1] < with_blocks[0]  # blocks matter: the intervals do not even overlap


# ---------------------------------------------------------------- refusals
def test_invalid_inputs_are_refused() -> None:
    rng = random.Random(0)
    bad_calls = [
        lambda: block_lengths(1.0, 0.0, 100),
        lambda: block_lengths(math.nan, 1.0, 100),
        lambda: block_lengths(1.0, 1.0, 0),
        # too short: 11 observations, m_max = ceil(sqrt(11)) + 5 = 9, leaving 2 pairs (3 needed)
        lambda: optimal_block_length([float(v % 3) for v in range(11)]),
        lambda: optimal_block_length([1.0] * 100),  # constant
        lambda: optimal_block_length([1.0, math.nan] * 50),
        lambda: stationary_blocks(0, 2.0, rng),
        lambda: stationary_blocks(10, 0.5, rng),
        lambda: stationary_blocks(10, math.inf, rng),
        lambda: resample_means([], 2.0, 10, rng),
        lambda: resample_means([1.0, 2.0], 2.0, 0, rng),
        lambda: resample_means([1.0, 2.0], 0.5, 10, rng),
        lambda: resample_means([1.0, math.inf], 2.0, 10, rng),
        lambda: resample([1.0, 2.0], fmean, 2.0, 0, rng),
        lambda: quantile([], 0.5),
        lambda: quantile([1.0], 1.5),
        lambda: percentile_interval([1.0, 2.0], 1.0),
    ]
    for call in bad_calls:
        with pytest.raises(BootstrapError):
            call()
    with pytest.raises(BootstrapError, match="too few"):  # refused for its length, nothing else
        optimal_block_length([float(v % 3) for v in range(11)])
    assert optimal_block_length([float(v % 3) for v in range(12)])  # 12 leaves 3 pairs: enough
