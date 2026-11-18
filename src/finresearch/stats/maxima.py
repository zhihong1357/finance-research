"""The best of many correlated trials when none has an edge (decision 0025).

The deflated Sharpe ratio compares a selected Sharpe ratio with what the best
of all the trials would show by luck alone. With ``N`` independent trials that
is the expected maximum of ``N`` standard normals; correlated trials find less,
and how much less depends on the whole correlation matrix. Instead of first
turning the matrix into an "effective number of trials" (ONC, or eigenvalue
rules; decision 0025 shows each fails on some structure), this module
simulates the quantity itself:

- ``expected_max_normal``: ``E[max_k Z_k]`` for ``Z ~ N(0, R)``, by Monte Carlo
  through a Cholesky factor of ``R`` (rank-deficient matrices allowed: a pivot
  that vanishes leaves its column at zero), with its standard error;
- ``null_benchmark``: the expected best per-period Sharpe ratio, that maximum
  times the standard error of a Sharpe ratio estimated from ``n`` observations
  under no edge, ``1 / sqrt(n - 1)`` (at a true Sharpe ratio of 0 skewness and
  kurtosis drop out, ``sharpe.sharpe_standard_error``);
- ``expected_max_iid`` and ``equivalent_trials``: the exact expected maximum of
  ``n`` independent standard normals, ``int x n phi(x) Phi(x)^(n-1) dx`` (real
  ``n >= 1``), and the ``n`` that matches a given maximum, so a report can say
  "as many as so many independent trials".
"""

import math
import random
from collections.abc import Sequence
from statistics import NormalDist

from finresearch.stats.montecarlo import Estimate, mean

_PIVOT = 1e-12  # a Cholesky pivot below this (relative to the diagonal) counts as zero
_GRID_HALF_WIDTH = 10.0  # the integral runs over [-10, 10]; beyond, Phi^n's tails are negligible
_GRID_STEPS = 4000  # Simpson's rule steps (even)
_BISECTION_STEPS = 100
_MAX_EQUIVALENT = 1e9
_NORMAL = NormalDist()
_MIN_DRAWS = 2


class MaximaError(ValueError):
    """The correlation matrix or the settings are not usable."""


def cholesky(corr: Sequence[Sequence[float]]) -> list[list[float]]:
    """Lower-triangular ``L`` with ``L L^T = corr`` for a positive semi-definite ``corr``."""
    n = len(corr)
    if n < 1 or any(len(row) != n for row in corr):
        raise MaximaError("the correlation matrix must be square and not empty")
    if not all(math.isfinite(v) for row in corr for v in row):
        raise MaximaError("correlations must be finite")
    low = [[0.0] * n for _ in range(n)]
    for j in range(n):
        d = corr[j][j] - math.fsum(low[j][k] ** 2 for k in range(j))
        if d < -_PIVOT * max(1.0, abs(corr[j][j])) * n:
            raise MaximaError("the matrix is not positive semi-definite")
        if d <= _PIVOT * max(1.0, abs(corr[j][j])):
            continue  # a dependent column: this direction adds nothing
        low[j][j] = math.sqrt(d)
        for i in range(j + 1, n):
            low[i][j] = (corr[i][j] - math.fsum(low[i][k] * low[j][k] for k in range(j))) / low[j][
                j
            ]
    return low


def expected_max_normal(
    corr: Sequence[Sequence[float]], draws: int, rng: random.Random
) -> Estimate:
    """``E[max_k Z_k]`` for ``Z ~ N(0, corr)``, by Monte Carlo, with its standard error."""
    if draws < _MIN_DRAWS:
        raise MaximaError(f"need at least {_MIN_DRAWS} draws, got {draws}")
    low = cholesky(corr)
    n = len(low)
    rows = [[(k, v) for k, v in enumerate(row) if v != 0.0] for row in low]
    maxima: list[float] = []
    for _ in range(draws):
        u = [rng.gauss(0.0, 1.0) for _ in range(n)]
        maxima.append(max(math.fsum(v * u[k] for k, v in row) for row in rows))
    return mean(maxima)


def null_benchmark(
    corr: Sequence[Sequence[float]], observations: int, draws: int, rng: random.Random
) -> Estimate:
    """The expected best per-period Sharpe ratio of the trials when none has an edge."""
    if observations < _MIN_DRAWS:
        raise MaximaError(f"need at least {_MIN_DRAWS} observations, got {observations}")
    z = expected_max_normal(corr, draws, rng)
    scale = 1.0 / math.sqrt(observations - 1)
    return Estimate(z.value * scale, z.standard_error * scale, z.n)


def expected_max_iid(n: float) -> float:
    """Exact ``E[max]`` of ``n`` independent standard normals (``n >= 1`` may be fractional)."""
    if not n >= 1.0 or not math.isfinite(n):
        raise MaximaError(f"need n >= 1, got {n}")
    if n == 1.0:
        return 0.0
    h = 2.0 * _GRID_HALF_WIDTH / _GRID_STEPS
    total = 0.0
    for step in range(_GRID_STEPS + 1):
        x = -_GRID_HALF_WIDTH + step * h
        weight = 1.0 if step in (0, _GRID_STEPS) else (4.0 if step % 2 else 2.0)
        total += weight * x * n * _NORMAL.pdf(x) * _NORMAL.cdf(x) ** (n - 1.0)
    return total * h / 3.0


def equivalent_trials(maximum: float) -> float:
    """The number of independent trials whose expected maximum is ``maximum`` (1 for <= 0)."""
    if not math.isfinite(maximum):
        raise MaximaError(f"the maximum must be finite, got {maximum}")
    if maximum <= 0.0:
        return 1.0
    lo, hi = 0.0, math.log(_MAX_EQUIVALENT)
    if expected_max_iid(_MAX_EQUIVALENT) < maximum:
        raise MaximaError(f"{maximum} is beyond {_MAX_EQUIVALENT:g} independent trials")
    for _ in range(_BISECTION_STEPS):
        mid = (lo + hi) / 2.0
        if expected_max_iid(math.exp(mid)) < maximum:
            lo = mid
        else:
            hi = mid
    return math.exp((lo + hi) / 2.0)
