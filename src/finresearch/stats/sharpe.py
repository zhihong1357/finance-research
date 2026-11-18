"""Inference on the Sharpe ratio: how sure we can be, and how much a search inflates it.

All Sharpe ratios here are per period (per day for daily returns); annualise
outside. ``skew`` and ``kurt`` are the third and fourth standardised moments
of the returns (``kurt`` is not excess kurtosis: 3 for a normal distribution).

- ``sharpe_standard_error``: the estimate's standard error with non-normal
  returns, ``sqrt((1 - skew SR + (kurt - 1) / 4 SR^2) / (n - 1))``.
- ``probabilistic_sharpe`` (Bailey & López de Prado 2012): the probability that
  the true Sharpe ratio exceeds ``benchmark``, ``Phi((SR - SR*) / se)``.
- ``min_track_record``: the number of observations needed for that probability
  to reach ``confidence``,
  ``1 + (1 - skew SR + (kurt - 1) / 4 SR^2) (z / (SR - SR*))^2``.
- ``expected_max_sharpe`` (Bailey & López de Prado 2014): the Sharpe ratio the
  best of ``trials`` independent trials with no skill is expected to show, given
  the variance of the trials' Sharpe ratios,
  ``sqrt(V) ((1 - g) Phi^-1(1 - 1/N) + g Phi^-1(1 - 1/(N e)))`` with ``g`` the
  Euler-Mascheroni constant.
- ``deflated_sharpe``: the probabilistic Sharpe ratio against that expected
  maximum: the chance the selected strategy beats what a search of that size
  finds by luck. ``trials`` is the effective number of independent trials
  (step 3.6b) and may be fractional.

``moments`` computes the inputs from a return series: the Sharpe ratio uses the
sample standard deviation (``n - 1``); skewness and kurtosis use the central
moments with ``1 / n``.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

EULER_GAMMA = 0.5772156649015329  # Euler-Mascheroni constant (a mathematical constant)
_MIN_OBSERVATIONS = 2
_NORMAL = NormalDist()


class SharpeError(ValueError):
    """The inputs do not support the requested statistic."""


@dataclass(frozen=True)
class Moments:
    n: int
    mean: float
    std: float  # sample standard deviation (n - 1)
    skew: float
    kurt: float  # not excess: 3 for a normal distribution

    @property
    def sharpe(self) -> float:
        return self.mean / self.std


def moments(returns: Sequence[float]) -> Moments:
    """Mean, sample standard deviation, skewness and kurtosis of ``returns``."""
    n = len(returns)
    if n < _MIN_OBSERVATIONS:
        raise SharpeError(f"need at least {_MIN_OBSERVATIONS} returns, got {n}")
    if not all(math.isfinite(r) for r in returns):
        raise SharpeError("returns must be finite")
    m = math.fsum(returns) / n
    d = [r - m for r in returns]
    m2 = math.fsum(x * x for x in d) / n
    if m2 == 0.0:
        raise SharpeError("constant returns have no Sharpe ratio")
    m3 = math.fsum(x**3 for x in d) / n
    m4 = math.fsum(x**4 for x in d) / n
    return Moments(n, m, math.sqrt(m2 * n / (n - 1)), m3 / m2**1.5, m4 / (m2 * m2))


def _spread(sr: float, skew: float, kurt: float) -> float:
    """``1 - skew SR + (kurt - 1) / 4 SR^2``: n times the variance of the estimate."""
    v = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if not v > 0.0:
        raise SharpeError(f"skew {skew} and kurt {kurt} give no positive variance at SR {sr}")
    return v


def sharpe_standard_error(sr: float, n: int, skew: float, kurt: float) -> float:
    """Standard error of an estimated Sharpe ratio from ``n`` non-normal returns."""
    if n < _MIN_OBSERVATIONS:
        raise SharpeError(f"need at least {_MIN_OBSERVATIONS} observations, got {n}")
    return math.sqrt(_spread(sr, skew, kurt) / (n - 1))


def probabilistic_sharpe(sr: float, benchmark: float, n: int, skew: float, kurt: float) -> float:
    """Probability that the true Sharpe ratio exceeds ``benchmark``."""
    return _NORMAL.cdf((sr - benchmark) / sharpe_standard_error(sr, n, skew, kurt))


def min_track_record(
    sr: float, benchmark: float, skew: float, kurt: float, confidence: float
) -> float:
    """Observations needed before ``probabilistic_sharpe`` reaches ``confidence``."""
    if sr <= benchmark:
        raise SharpeError(f"SR {sr} must exceed the benchmark {benchmark}")
    if not 0.0 < confidence < 1.0:
        raise SharpeError(f"confidence must be strictly between 0 and 1, got {confidence}")
    z = _NORMAL.inv_cdf(confidence)
    return 1.0 + _spread(sr, skew, kurt) * (z / (sr - benchmark)) ** 2


def expected_max_sharpe(trials: float, variance: float) -> float:
    """Expected best Sharpe ratio among ``trials`` independent trials with no skill."""
    if not trials > 1.0:
        raise SharpeError(f"need more than one trial, got {trials}")
    if variance < 0.0:
        raise SharpeError(f"variance must not be negative, got {variance}")
    a = _NORMAL.inv_cdf(1.0 - 1.0 / trials)
    b = _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return math.sqrt(variance) * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe(
    sr: float, n: int, skew: float, kurt: float, trials: float, variance: float
) -> float:
    """Probability that the selected Sharpe ratio beats the best that luck would find."""
    return probabilistic_sharpe(sr, expected_max_sharpe(trials, variance), n, skew, kurt)
