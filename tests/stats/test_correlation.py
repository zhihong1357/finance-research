import math

import pytest

from finresearch.stats.correlation import StatsError, fisher_interval, pearson, t_statistic

X, Y = [1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 5.0, 9.0]


def test_pearson_by_hand() -> None:
    # deviations: x (-1.5, -0.5, 0.5, 1.5), y (-3, -1, 0, 4); Sxy = 11, Sxx = 5, Syy = 26
    assert pearson(X, Y) == pytest.approx(11 / math.sqrt(130))
    assert pearson(X, [-v for v in Y]) == pytest.approx(-11 / math.sqrt(130))
    assert pearson(X, [10.0 * v + 3.0 for v in X]) == pytest.approx(1.0)


def test_t_statistic_by_hand() -> None:
    # r = 11/sqrt(130), 1 - r^2 = 9/130, so t = r * sqrt(2 / (9/130)) = 11 * sqrt(2) / 3
    assert t_statistic(11 / math.sqrt(130), 4) == pytest.approx(11 * math.sqrt(2) / 3)
    assert t_statistic(0.0, 100) == 0.0


def test_fisher_interval_by_hand() -> None:
    # r = 0, n = 7: atanh(0) = 0, half-width 1.959964 / 2, tanh(0.979982) = 0.753058
    low, high = fisher_interval(0.0, 7, 0.95)
    assert (low, high) == (pytest.approx(-0.753058), pytest.approx(0.753058))
    # r = 0.5, n = 28: atanh(0.5) = 0.549306 +/- 0.391993 -> tanh(0.157313), tanh(0.941299)
    low, high = fisher_interval(0.5, 28, 0.95)
    assert (low, high) == (pytest.approx(0.156028, abs=1e-6), pytest.approx(0.735818, abs=1e-6))
    narrow = fisher_interval(0.5, 28, 0.50)
    assert low < narrow[0] < 0.5 < narrow[1] < high


def test_refuses_what_it_cannot_compute() -> None:
    with pytest.raises(StatsError, match="has 4 values"):
        pearson(X, Y[:3])
    with pytest.raises(StatsError, match="at least 4"):
        pearson(X[:3], Y[:3])
    with pytest.raises(StatsError, match="constant"):
        pearson(X, [1.0] * 4)
    with pytest.raises(StatsError, match="strictly between"):
        t_statistic(1.0, 10)
    with pytest.raises(StatsError, match="at least 4"):
        fisher_interval(0.1, 3, 0.95)
    with pytest.raises(StatsError, match="confidence"):
        fisher_interval(0.1, 10, 1.0)
