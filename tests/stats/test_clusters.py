import math
import random

import pytest

from finresearch.stats.clusters import (
    ClusterError,
    Clustering,
    cluster_returns,
    clustered_sharpe_variance,
    correlation_matrix,
    onc,
    silhouettes,
)


def _line(points: list[float]) -> list[list[float]]:
    return [[abs(a - b) for b in points] for a in points]


def block_matrix(sizes: list[int], inside: float, outside: float) -> list[list[float]]:
    labels = [b for b, size in enumerate(sizes) for _ in range(size)]
    n = len(labels)
    return [
        [1.0 if i == j else inside if labels[i] == labels[j] else outside for j in range(n)]
        for i in range(n)
    ]


# ---------------------------------------------------------------- the pieces by hand
def test_silhouettes_by_hand() -> None:
    # points 0, 1, 10, 11 on a line, two clusters {0, 1} and {10, 11}:
    # point 0: a = 1, b = (10 + 11) / 2 = 10.5, s = 9.5 / 10.5 = 0.904762
    # point 1: a = 1, b = (9 + 10) / 2 = 9.5, s = 8.5 / 9.5 = 0.894737 (and mirrored)
    s = silhouettes(_line([0.0, 1.0, 10.0, 11.0]), [0, 0, 1, 1])
    assert s == pytest.approx([0.904762, 0.894737, 0.894737, 0.904762], abs=1e-6)
    # {0, 1, 10} and {11} alone: point 0: a = (1 + 10) / 2 = 5.5, b = 11, s = 5.5 / 11 = 0.5;
    # point 1: a = (1 + 9) / 2 = 5, b = 10, s = 0.5; point 10: a = (10 + 9) / 2 = 9.5, b = 1,
    # s = (1 - 9.5) / 9.5 = -0.894737; 11 alone: 0
    s = silhouettes(_line([0.0, 1.0, 10.0, 11.0]), [0, 0, 0, 1])
    assert s == pytest.approx([0.5, 0.5, -0.894737, 0.0], abs=1e-6)


def test_correlation_matrix_by_hand() -> None:
    corr = correlation_matrix([[1.0, 2.0, 3.0], [2.0, 4.0, 6.0], [3.0, 2.0, 1.0], [1.0, 3.0, 2.0]])
    # the fourth against the first: deviations (-1, 1, 0) and (-1, 0, 1): 1 / (sqrt 2 sqrt 2) = 0.5
    assert corr[0][:3] == pytest.approx([1.0, 1.0, -1.0])
    assert corr[0][3] == pytest.approx(0.5)
    assert corr[2][3] == pytest.approx(-0.5)
    assert all(corr[i][j] == corr[j][i] for i in range(4) for j in range(4))


def test_cluster_returns_and_variance_by_hand() -> None:
    # one cluster of a = [1, 3] (variance 2) and b = [0, 4] (variance 8): inverse-variance
    # weights 1/2 : 1/8 = 0.8 : 0.2, combined [0.8, 0.8 x 3 + 0.2 x 4] = [0.8, 3.2]
    together = Clustering(((0, 1),), (0.0, 0.0), math.nan)
    assert cluster_returns([[1.0, 3.0], [0.0, 4.0]], together) == [pytest.approx([0.8, 3.2])]
    # two singletons: [1, 2, 3, 4] has Sharpe 2.5 / 1.290994 = 1.936492; [0, 1, 0, 1] has
    # 0.5 / 0.577350 = 0.866025; their sample variance (1.070466)^2 / 1 / 2 = 0.572949
    apart = Clustering(((0,), (1,)), (0.0, 0.0), math.nan)
    series = [[1.0, 2.0, 3.0, 4.0], [0.0, 1.0, 0.0, 1.0]]
    assert clustered_sharpe_variance(series, apart) == pytest.approx(0.572949, abs=1e-6)


# ---------------------------------------------------------------- ONC
def test_clear_blocks_are_found_exactly() -> None:
    corr = block_matrix([3, 4, 5], inside=0.9, outside=0.1)
    c = onc(corr, random.Random(1))
    assert c.clusters == ((0, 1, 2), (3, 4, 5, 6), (7, 8, 9, 10, 11))
    assert c.k == 3
    assert c.labels(12) == [0] * 3 + [1] * 4 + [2] * 5


def test_the_same_seed_gives_the_same_clustering() -> None:
    corr, _ = random_block_corr(20, 4, 2, random.Random(3))
    assert onc(corr, random.Random(5)) == onc(corr, random.Random(5))


def _cov_sub(n_obs: int, n_cols: int, sigma: float, rng: random.Random) -> list[list[float]]:
    """Snippet 3's ``getCovSub``: one factor copied to every column, plus noise of ``sigma``."""
    base = [rng.gauss(0.0, 1.0) for _ in range(n_obs)]
    cols = [[b + rng.gauss(0.0, sigma) for b in base] for _ in range(n_cols)]
    means = [math.fsum(c) / n_obs for c in cols]
    return [
        [
            math.fsum((cols[i][t] - means[i]) * (cols[j][t] - means[j]) for t in range(n_obs))
            / (n_obs - 1)
            for j in range(n_cols)
        ]
        for i in range(n_cols)
    ]


def _block_cov(
    n: int, k: int, m: int, sigma: float, rng: random.Random
) -> tuple[list[list[float]], list[int]]:
    """Snippet 3's ``getRndBlockCov``: ``k`` blocks of at least ``m`` in random sizes."""
    spare = n - (m - 1) * k
    cuts = [*sorted(rng.sample(range(1, spare), k - 1)), spare]
    sizes = [b - a - 1 + m for a, b in zip([0, *cuts[:-1]], cuts, strict=True)]
    cov = [[0.0] * n for _ in range(n)]
    offset = 0
    for size in sizes:
        sub = [[1.0]] if size == 1 else _cov_sub(max(size * (size + 1) // 2, 100), size, sigma, rng)
        for i in range(size):
            for j in range(size):
                cov[offset + i][offset + j] = sub[i][j]
        offset += size
    return cov, sizes


def random_block_corr(
    n: int, k: int, m: int, rng: random.Random
) -> tuple[list[list[float]], list[int]]:
    """Snippet 3's ``randomBlockCorr``: ``k`` blocks (noise 0.5) plus one block of all (noise 1)."""
    blocks, sizes = _block_cov(n, k, m, 0.5, rng)
    common, _ = _block_cov(n, 1, m, 1.0, rng)
    cov = [[blocks[i][j] + common[i][j] for j in range(n)] for i in range(n)]
    sd = [math.sqrt(cov[i][i]) for i in range(n)]
    corr = [[max(-1.0, min(1.0, cov[i][j] / (sd[i] * sd[j]))) for j in range(n)] for i in range(n)]
    return corr, sizes


# ---------------------------------------------------------------- step 3.6b acceptance
@pytest.mark.parametrize(("n", "k", "matrices"), [(20, 3, 20), (20, 6, 20), (40, 9, 8)])
def test_onc_recovers_the_number_of_blocks(n: int, k: int, matrices: int) -> None:
    # The paper's experiment (section 9): random block correlation matrices with K blocks of at
    # least 2 (Snippet 3). Run on the same 40 matrices, the paper's own code (scikit-learn) found
    # K exactly in all of them for (20, 6) and (40, 9), and so did this implementation. Here:
    # exact in at least 90%, and never off by more than one cluster.
    rng = random.Random(1000 + n + k)
    found = [onc(random_block_corr(n, k, 2, rng)[0], rng).k for _ in range(matrices)]
    assert sum(1 for f in found if f == k) >= 0.9 * matrices
    assert all(abs(f - k) <= 1 for f in found)


def test_invalid_inputs_are_refused() -> None:
    rng = random.Random(0)
    bad_calls = [
        lambda: onc([[1.0, 0.5], [0.5, 1.0]], rng),  # two trials: k would run over 2 .. 1
        lambda: onc([[1.0, 0.5, 0.5], [0.5, 1.0, 0.5]], rng),  # not square
        lambda: onc([[1.0, 0.5, 0.2], [0.4, 1.0, 0.5], [0.2, 0.5, 1.0]], rng),  # not symmetric
        lambda: onc([[1.0, 1.5, 0.2], [1.5, 1.0, 0.5], [0.2, 0.5, 1.0]], rng),  # beyond 1
        lambda: onc(block_matrix([2, 2], 0.9, 0.1), rng, n_init=0),
        lambda: correlation_matrix([[1.0, 2.0]]),
        lambda: correlation_matrix([[1.0, 2.0], [1.0, 1.0]]),  # constant
        lambda: correlation_matrix([[1.0, 2.0], [1.0, 2.0, 3.0]]),
        lambda: silhouettes(_line([0.0, 1.0]), [0, 0]),  # one cluster
        lambda: clustered_sharpe_variance([[1.0, 2.0]], Clustering(((0,),), (0.0,), math.nan)),
        lambda: cluster_returns(
            [[1.0, 1.0], [0.0, 4.0]], Clustering(((0, 1),), (0.0, 0.0), math.nan)
        ),
    ]
    for call in bad_calls:
        with pytest.raises(ClusterError):
            call()
