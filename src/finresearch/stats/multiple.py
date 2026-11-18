"""Multiple testing: adjust a family of p-values so the family keeps its error rate.

Each method returns adjusted p-values in the order of the input; hypothesis ``i``
is rejected at level ``alpha`` when its adjusted p-value is at most ``alpha``
(``reject``). With the p-values sorted, ``p_(1) <= ... <= p_(m)``:

- **Holm** (1979), step-down, controls the family-wise error rate (the chance
  of any false rejection) under any dependence:
  ``adj_(i) = max_{j <= i} min(1, (m - j + 1) p_(j))``.
- **Benjamini-Hochberg** (1995), step-up, controls the false discovery rate
  (the expected share of false rejections among rejections) at ``alpha m0 / m``
  for independent tests, and also under positive regression dependency
  (Benjamini & Yekutieli 2001): ``adj_(i) = min_{j >= i} min(1, m p_(j) / j)``.
- **Benjamini-Yekutieli** (2001), the same step-up with every term multiplied by
  ``c(m) = 1 + 1/2 + ... + 1/m``: controls the false discovery rate under any
  dependence, at the price of power.

Ties keep their input order (the sort is stable); the adjusted values do not
depend on how ties are broken.
"""

import math
from collections.abc import Callable, Sequence
from enum import StrEnum


class Method(StrEnum):
    HOLM = "holm"
    BH = "benjamini-hochberg"
    BY = "benjamini-yekutieli"


class MultipleTestingError(ValueError):
    """The p-values or the level are not valid."""


def _check(p_values: Sequence[float]) -> None:
    for i, p in enumerate(p_values):
        if not (math.isfinite(p) and 0.0 <= p <= 1.0):
            raise MultipleTestingError(f"p-value {i} must be in [0, 1], got {p}")


def _order(p_values: Sequence[float]) -> list[int]:
    return sorted(range(len(p_values)), key=lambda i: p_values[i])


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm's step-down adjusted p-values."""
    _check(p_values)
    m = len(p_values)
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(_order(p_values)):  # rank = j - 1
        running = max(running, min(1.0, (m - rank) * p_values[i]))
        adjusted[i] = running
    return adjusted


def _step_up(p_values: Sequence[float], factor: float) -> list[float]:
    _check(p_values)
    m = len(p_values)
    adjusted = [0.0] * m
    running = 1.0
    order = _order(p_values)
    for rank in range(m - 1, -1, -1):  # from the largest p-value down
        i = order[rank]
        # m / j first: it is exactly 1 at the largest rank, so rounding never pushes a value below p
        running = min(running, min(1.0, factor * p_values[i] * (m / (rank + 1))))
        adjusted[i] = running
    return adjusted


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg step-up adjusted p-values."""
    return _step_up(p_values, 1.0)


def harmonic(m: int) -> float:
    """``1 + 1/2 + ... + 1/m``, the Benjamini-Yekutieli constant ``c(m)``."""
    if m < 0:
        raise MultipleTestingError(f"m must be non-negative, got {m}")
    return math.fsum(1.0 / k for k in range(1, m + 1))


def benjamini_yekutieli(p_values: Sequence[float]) -> list[float]:
    """Benjamini-Yekutieli step-up adjusted p-values (any dependence)."""
    return _step_up(p_values, harmonic(len(p_values)))


ADJUST: dict[Method, Callable[[Sequence[float]], list[float]]] = {
    Method.HOLM: holm,
    Method.BH: benjamini_hochberg,
    Method.BY: benjamini_yekutieli,
}


def adjust(p_values: Sequence[float], method: Method) -> list[float]:
    """Adjusted p-values by ``method``, in input order."""
    return ADJUST[method](p_values)


def reject(p_values: Sequence[float], alpha: float, method: Method) -> list[bool]:
    """Which hypotheses ``method`` rejects at level ``alpha``."""
    if not 0.0 < alpha < 1.0:
        raise MultipleTestingError(f"alpha must be strictly between 0 and 1, got {alpha}")
    return [a <= alpha for a in adjust(p_values, method)]
