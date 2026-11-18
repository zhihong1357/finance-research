"""Power: the chance a test finds an effect that is really there, and the data that takes.

Hypothesis cards state their power before any data is seen (protocol section
2); these functions are what that statement is computed with. All use the
normal approximation to the test statistic.

- **A Sharpe ratio** (one series): the test statistic is the estimated Sharpe
  ratio over its standard error (``sharpe.sharpe_standard_error``, so skewness
  and fat tails count). With true per-period Sharpe ratio ``s`` and ``n_eff``
  effective observations, ``power = Phi(s / se - t) + Phi(-s / se - t)``
  (two-sided; drop the second term for one side).
- **Two groups** of ``n`` each with standardised difference ``d`` (Cohen's d):
  ``power = Phi(d sqrt(n / 2) - t) + Phi(-d sqrt(n / 2) - t)``; per group
  ``n = 2 ((t + z_power) / d)^2``.
- **Dependence** shrinks the information in ``n`` observations to
  ``n_eff = n / inflation``, where ``inflation`` is the ratio of the long-run
  variance to the variance: ``(1 + rho) / (1 - rho)`` for AR(1) returns, and
  ``h`` for overlapping ``h``-period labels built from independent returns.

``required_*`` invert ``power`` for one side (the far tail adds almost nothing
at useful power) and round up, never below the 2 (effective) observations the
statistic needs: a huge effect still needs a sample that can show it.
"""

import math
from statistics import NormalDist

from finresearch.stats.sharpe import sharpe_standard_error

_NORMAL = NormalDist()
_MIN_OBSERVATIONS = 2


class PowerError(ValueError):
    """The inputs do not describe a test that power can be computed for."""


def _check_threshold(threshold: float) -> None:
    if not threshold > 0.0:
        raise PowerError(f"threshold must be positive, got {threshold}")


def _check_power(power: float) -> None:
    if not 0.0 < power < 1.0:
        raise PowerError(f"power must be strictly between 0 and 1, got {power}")


def ar1_inflation(rho: float) -> float:
    """Long-run variance over variance for AR(1) returns with autocorrelation ``rho``."""
    if not -1.0 < rho < 1.0:
        raise PowerError(f"rho must be strictly between -1 and 1, got {rho}")
    return (1.0 + rho) / (1.0 - rho)


def overlap_inflation(horizon: int) -> float:
    """Long-run variance over variance for overlapping ``horizon``-period sums of iid returns."""
    if horizon < 1:
        raise PowerError(f"horizon must be at least 1, got {horizon}")
    return float(horizon)


def _effective(n: float, inflation: float) -> float:
    if inflation <= 0.0:
        raise PowerError(f"inflation must be positive, got {inflation}")
    n_eff = n / inflation
    if n_eff < _MIN_OBSERVATIONS:
        raise PowerError(f"{n} observations at inflation {inflation} leave fewer than 2")
    return n_eff


def sharpe_power(
    sr: float,
    n: int,
    threshold: float,
    *,
    skew: float = 0.0,
    kurt: float = 3.0,
    inflation: float = 1.0,
    two_sided: bool = True,
) -> float:
    """Probability that ``|SR / se| >= threshold`` (or ``SR / se >= threshold`` one-sided)."""
    _check_threshold(threshold)
    n_eff = _effective(n, inflation)
    # se^2 (n - 1) does not depend on n; n_eff may be fractional, so scale it from n = 2
    se = sharpe_standard_error(sr, _MIN_OBSERVATIONS, skew, kurt) / math.sqrt(n_eff - 1.0)
    delta = sr / se
    upper = _NORMAL.cdf(delta - threshold)
    return upper + _NORMAL.cdf(-delta - threshold) if two_sided else upper


def required_sharpe_observations(
    sr: float,
    threshold: float,
    power: float,
    *,
    skew: float = 0.0,
    kurt: float = 3.0,
    inflation: float = 1.0,
) -> int:
    """Observations for ``SR / se >= threshold`` with probability ``power``."""
    _check_threshold(threshold)
    _check_power(power)
    if not sr > 0.0:
        raise PowerError(f"the effect must be positive, got SR {sr}")
    if inflation <= 0.0:
        raise PowerError(f"inflation must be positive, got {inflation}")
    spread = sharpe_standard_error(sr, _MIN_OBSERVATIONS, skew, kurt) ** 2  # (n - 1) se^2
    n_eff = max(_MIN_OBSERVATIONS, 1.0 + spread * ((threshold + _NORMAL.inv_cdf(power)) / sr) ** 2)
    return math.ceil(n_eff * inflation)


def two_sample_power(
    d: float, n_per_group: int, threshold: float, *, two_sided: bool = True
) -> float:
    """Probability that a two-group comparison with effect ``d`` reaches ``threshold``."""
    _check_threshold(threshold)
    if n_per_group < _MIN_OBSERVATIONS:
        raise PowerError(f"need at least {_MIN_OBSERVATIONS} per group, got {n_per_group}")
    delta = d * math.sqrt(n_per_group / 2.0)
    upper = _NORMAL.cdf(delta - threshold)
    return upper + _NORMAL.cdf(-delta - threshold) if two_sided else upper


def required_per_group(d: float, threshold: float, power: float) -> int:
    """Observations per group for a two-group comparison: ``2 ((t + z) / d)^2``, rounded up."""
    _check_threshold(threshold)
    _check_power(power)
    if not d > 0.0:
        raise PowerError(f"the effect must be positive, got d {d}")
    n = math.ceil(2.0 * ((threshold + _NORMAL.inv_cdf(power)) / d) ** 2)
    return max(_MIN_OBSERVATIONS, n)
