"""Monte Carlo estimates always come with their own error.

A simulation that runs ``n`` times estimates a probability or a mean; with ``n``
finite the estimate is itself noisy, so every estimate carries its standard
error, and a measured rate is compared with a nominal level through an interval,
never by its point value alone.

- ``proportion``: share of ``hits`` in ``n`` runs, standard error
  ``sqrt(p (1 - p) / n)``.
- ``wilson_interval``: the Wilson score interval for that share. Unlike
  ``p +/- z se`` it stays inside [0, 1] and is not empty when there are no hits:
  0 hits in ``n`` runs still bound the rate by about ``z^2 / (n + z^2)``. That is
  the case that matters for false positive rates near 0.27%.
- ``mean``: sample mean, standard error ``s / sqrt(n)`` with the ``n - 1``
  sample variance.
- ``runs_for_relative_error``: how many runs make the standard error of a share
  near ``p`` a given fraction of ``p``: ``n = (1 - p) / (p r^2)``.

Sums use ``math.fsum`` so the results do not depend on summation order.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import NormalDist

_MIN_FOR_MEAN = 2  # the sample variance needs n - 1 > 0


class EstimateError(ValueError):
    """The inputs do not support the requested estimate."""


@dataclass(frozen=True)
class Estimate:
    value: float
    standard_error: float
    n: int


def _z(confidence: float) -> float:
    if not 0.0 < confidence < 1.0:
        raise EstimateError(f"confidence must be strictly between 0 and 1, got {confidence}")
    return NormalDist().inv_cdf(1.0 - (1.0 - confidence) / 2.0)


def _check_counts(hits: int, n: int) -> None:
    if n <= 0:
        raise EstimateError(f"need at least one run, got n = {n}")
    if not 0 <= hits <= n:
        raise EstimateError(f"hits must be between 0 and n = {n}, got {hits}")


def proportion(hits: int, n: int) -> Estimate:
    """The share of runs that hit, with its binomial standard error."""
    _check_counts(hits, n)
    p = hits / n
    return Estimate(p, math.sqrt(p * (1.0 - p) / n), n)


def wilson_interval(hits: int, n: int, confidence: float) -> tuple[float, float]:
    """Wilson score interval for the share ``hits / n``."""
    _check_counts(hits, n)
    z = _z(confidence)
    p, z2 = hits / n, z * z
    scale = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / scale
    half = z / scale * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def mean(values: Iterable[float]) -> Estimate:
    """Sample mean with the standard error of the mean."""
    xs = [float(v) for v in values]
    n = len(xs)
    if n < _MIN_FOR_MEAN:
        raise EstimateError(f"need at least {_MIN_FOR_MEAN} values, got {n}")
    if not all(math.isfinite(x) for x in xs):
        raise EstimateError("values must be finite")
    m = math.fsum(xs) / n
    variance = math.fsum((x - m) ** 2 for x in xs) / (n - 1)
    return Estimate(m, math.sqrt(variance / n), n)


def runs_for_relative_error(p: float, relative_error: float) -> int:
    """Runs needed so a share near ``p`` has standard error ``relative_error * p``."""
    if not 0.0 < p < 1.0:
        raise EstimateError(f"p must be strictly between 0 and 1, got {p}")
    if relative_error <= 0.0:
        raise EstimateError(f"relative_error must be positive, got {relative_error}")
    return math.ceil((1.0 - p) / (p * relative_error * relative_error))
