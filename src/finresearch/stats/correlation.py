"""Pearson correlation, its t statistic and a Fisher-z confidence interval.

Sums use ``math.fsum`` (exactly rounded), so the result does not depend on the
order of accumulation beyond the inputs themselves.
"""

import math
from collections.abc import Sequence
from statistics import NormalDist

_MIN_PAIRS = 4  # the Fisher interval needs n - 3 > 0, the t statistic n - 2 > 0


class StatsError(ValueError):
    """The inputs do not support the requested statistic."""


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y):
        raise StatsError(f"x has {len(x)} values, y has {len(y)}")
    n = len(x)
    if n < _MIN_PAIRS:
        raise StatsError(f"need at least {_MIN_PAIRS} pairs, got {n}")
    mx, my = math.fsum(x) / n, math.fsum(y) / n
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    sxx = math.fsum(d * d for d in dx)
    syy = math.fsum(d * d for d in dy)
    if sxx == 0 or syy == 0:
        raise StatsError("a constant series has no correlation")
    sxy = math.fsum(a * b for a, b in zip(dx, dy, strict=True))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def _check(r: float, n: int) -> None:
    if n < _MIN_PAIRS:
        raise StatsError(f"need at least {_MIN_PAIRS} pairs, got {n}")
    if not -1.0 < r < 1.0:
        raise StatsError(f"correlation must be strictly between -1 and 1, got {r}")


def t_statistic(r: float, n: int) -> float:
    """``r * sqrt((n - 2) / (1 - r^2))``: t with n - 2 degrees of freedom under no correlation."""
    _check(r, n)
    return r * math.sqrt((n - 2) / (1.0 - r * r))


def fisher_interval(r: float, n: int, confidence: float) -> tuple[float, float]:
    """Two-sided interval from ``atanh(r) +/- z / sqrt(n - 3)``, mapped back with ``tanh``."""
    _check(r, n)
    if not 0.0 < confidence < 1.0:
        raise StatsError(f"confidence must be between 0 and 1, got {confidence}")
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    centre, half = math.atanh(r), z / math.sqrt(n - 3)
    return math.tanh(centre - half), math.tanh(centre + half)
