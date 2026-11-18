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
- ``detection_power`` and ``runs_to_detect`` (decision 0023): a measured rate
  counts as above a nominal level when the Wilson interval's lower bound is
  above it (decision 0018). ``detection_power`` is the exact binomial chance of
  that when the true rate is ``true_rate``; ``runs_to_detect`` the fewest runs
  from which that chance never drops below ``power`` again.

Sums use ``math.fsum`` so the results do not depend on summation order.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import NormalDist

_MIN_FOR_MEAN = 2  # the sample variance needs n - 1 > 0
_HORIZON = 2  # runs_to_detect looks this many times the normal-approximation count ahead
_TAIL_STOP = 1e-17  # stop summing binomial terms once they add less than this share


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


def _wilson(hits: int, n: int, z: float) -> tuple[float, float]:
    p, z2 = hits / n, z * z
    scale = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / scale
    half = z / scale * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    # exact at the ends (0 hits bound the rate below by 0, n hits above by 1), and never
    # excluding the share itself, whatever the rounding
    low = 0.0 if hits == 0 else min(p, max(0.0, center - half))
    high = 1.0 if hits == n else max(p, min(1.0, center + half))
    return low, high


def wilson_interval(hits: int, n: int, confidence: float) -> tuple[float, float]:
    """Wilson score interval for the share ``hits / n``."""
    _check_counts(hits, n)
    return _wilson(hits, n, _z(confidence))


def mean(values: Iterable[float]) -> Estimate:
    """Sample mean with the standard error of the mean."""
    xs = [float(v) for v in values]
    n = len(xs)
    if n < _MIN_FOR_MEAN:
        raise EstimateError(f"need at least {_MIN_FOR_MEAN} values, got {n}")
    if not all(math.isfinite(x) for x in xs):
        raise EstimateError("values must be finite")
    # scale by a power of two (exact) so that sums and squares of huge values cannot overflow
    exponent = math.frexp(max(abs(x) for x in xs))[1]
    ys = [math.ldexp(x, -exponent) for x in xs]  # now |y| < 1
    m = math.fsum(ys) / n
    variance = math.fsum((y - m) ** 2 for y in ys) / (n - 1)
    return Estimate(math.ldexp(m, exponent), math.ldexp(math.sqrt(variance / n), exponent), n)


def runs_for_relative_error(p: float, relative_error: float) -> int:
    """Runs needed so a share near ``p`` has standard error ``relative_error * p``."""
    if not 0.0 < p < 1.0:
        raise EstimateError(f"p must be strictly between 0 and 1, got {p}")
    if relative_error <= 0.0:
        raise EstimateError(f"relative_error must be positive, got {relative_error}")
    return math.ceil((1.0 - p) / (p * relative_error * relative_error))


def _check_rates(nominal: float, true_rate: float) -> None:
    if not 0.0 < nominal < true_rate < 1.0:
        raise EstimateError(
            f"need 0 < nominal < true rate < 1, got nominal {nominal}, true rate {true_rate}"
        )


def _critical_hits(nominal: float, n: int, z: float) -> int:
    """Fewest hits in ``n`` runs whose Wilson lower bound is above ``nominal`` (n + 1: none)."""
    lo, hi = 0, n + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if _wilson(mid, n, z)[0] > nominal:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _log_pmf(j: int, n: int, p: float) -> float:
    return (
        math.lgamma(n + 1)
        - math.lgamma(j + 1)
        - math.lgamma(n - j + 1)
        + j * math.log(p)
        + (n - j) * math.log1p(-p)
    )


def _tail(start: int, step: int, n: int, p: float) -> float:
    """Binomial probabilities summed from ``start`` outwards (``step`` +1 up, -1 down)."""
    term, terms, running = math.exp(_log_pmf(start, n, p)), list[float](), 0.0
    j, mode = start, n * p
    while 0 <= j <= n:
        terms.append(term)
        running += term
        past_the_mode = j > mode if step > 0 else j < mode
        if past_the_mode and term <= _TAIL_STOP * running:  # also stops on underflow to 0
            break
        if step > 0:
            term *= (n - j) / (j + 1) * p / (1.0 - p)
        else:
            term *= j / (n - j + 1) * (1.0 - p) / p
        j += step
    return math.fsum(terms)


def _upper_tail(k: int, n: int, p: float) -> float:
    """``P(X >= k)`` for ``X ~ Binomial(n, p)``, summing whichever tail is the short one."""
    if k > n:
        return 0.0
    if k <= 0:
        return 1.0
    if k >= n * p:
        return min(1.0, _tail(k, +1, n, p))
    return max(0.0, 1.0 - _tail(k - 1, -1, n, p))


def detection_power(nominal: float, true_rate: float, n: int, confidence: float) -> float:
    """Chance that ``n`` runs at ``true_rate`` put the Wilson lower bound above ``nominal``."""
    _check_rates(nominal, true_rate)
    if n <= 0:
        raise EstimateError(f"need at least one run, got n = {n}")
    return _upper_tail(_critical_hits(nominal, n, _z(confidence)), n, true_rate)


def runs_to_detect(nominal: float, true_rate: float, power: float, confidence: float) -> int:
    """Fewest runs from which ``detection_power`` stays at or above ``power``.

    The exact power is a sawtooth in ``n``: while the number of hits needed stays
    the same it rises, and it drops each time one more hit becomes necessary. So
    the answer is checked at every such drop up to ``_HORIZON`` times the normal
    approximation's count, beyond which the power is far above any target.
    """
    _check_rates(nominal, true_rate)
    if not 0.0 < power < 1.0:
        raise EstimateError(f"power must be strictly between 0 and 1, got {power}")
    z = _z(confidence)
    z_power = NormalDist().inv_cdf(power)
    spread = z * math.sqrt(nominal * (1.0 - nominal)) + z_power * math.sqrt(
        true_rate * (1.0 - true_rate)
    )
    horizon = _HORIZON * math.ceil((spread / (true_rate - nominal)) ** 2)
    # walk the stretches of n that need the same number of hits; within one the power rises
    start, last_short = 1, 0
    while start <= horizon:
        k = _critical_hits(nominal, start, z)
        lo, hi = start, horizon + 1  # the first n that needs more than k hits
        while lo < hi:
            mid = (lo + hi) // 2
            if _critical_hits(nominal, mid, z) > k:
                hi = mid
            else:
                lo = mid + 1
        if _upper_tail(k, start, true_rate) < power:  # find the last short n in this stretch
            a, b = start, lo - 1
            while a < b:
                mid = (a + b + 1) // 2
                if _upper_tail(k, mid, true_rate) < power:
                    a = mid
                else:
                    b = mid - 1
            last_short = a
        start = lo
    return last_short + 1
