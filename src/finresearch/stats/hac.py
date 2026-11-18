"""t statistics for means of dependent series (decision 0024, choice 2).

A strategy held for ``H`` days and rebalanced daily is the average of ``H``
overlapping cohorts, so its daily returns are correlated up to lag ``H - 1``.
Treating them as independent (``ordinary_t``) overstates the t statistic by
about ``sqrt(H)``. Decision 0024 has the meta-research measure two remedies:

- ``newey_west_t``: the mean over the Newey-West standard error, with the
  long-run variance ``g0 + 2 sum_{l=1}^{L} (1 - l / (L + 1)) g_l`` (Bartlett
  weights, autocovariances ``g_l`` with denominator ``n``), lag ``L = H - 1``;
- the ordinary t statistic of non-overlapping returns: one cohort formed every
  ``H`` days and held ``H`` days (built by the bench, ``meta.bench``).

Two traps, both found while testing this module:

- The Bartlett weights cost Newey-West exactly here. For an ``H``-day overlap of
  independent returns (an MA(H - 1)), lag ``H - 1`` recovers only
  ``sum_{|l|<H} (H - |l|)^2 / H^3 = (2 H^2 + 1) / (3 H^2)`` of the long-run
  variance: 68% for ``H = 5``, about two thirds for large ``H``. The t statistic
  is then too large by ``sqrt(3 H^2 / (2 H^2 + 1))``, 1.21 for ``H = 5``.
- Summing the overlapping portfolio's daily returns over consecutive ``H``-day
  blocks does not make them independent: the cohort formed at a block's start
  runs into the next block (for ``H = 5`` neighbouring blocks correlate 0.235),
  and the t statistic of those sums is too large by the same 1.21. Non-overlapping
  returns must come from single cohorts, not from blocks of the average.
"""

import math
from collections.abc import Sequence

_MIN_POINTS = 2


class HacError(ValueError):
    """The series does not support the requested statistic."""


def _centred(x: Sequence[float]) -> tuple[float, list[float]]:
    if len(x) < _MIN_POINTS:
        raise HacError(f"need at least {_MIN_POINTS} observations, got {len(x)}")
    if not all(math.isfinite(v) for v in x):
        raise HacError("the series must be finite")
    m = math.fsum(x) / len(x)
    return m, [v - m for v in x]


def long_run_variance(x: Sequence[float], lag: int) -> float:
    """Newey-West (Bartlett) long-run variance of ``x`` with ``lag`` lags."""
    if lag < 0:
        raise HacError(f"lag must not be negative, got {lag}")
    _, d = _centred(x)
    n = len(d)
    if lag >= n:
        raise HacError(f"lag {lag} needs more than {lag} observations, got {n}")
    total = math.fsum(v * v for v in d) / n
    for lag_l in range(1, lag + 1):
        gamma = math.fsum(d[t] * d[t - lag_l] for t in range(lag_l, n)) / n
        total += 2.0 * (1.0 - lag_l / (lag + 1)) * gamma
    return total


def newey_west_t(x: Sequence[float], lag: int) -> float:
    """``mean / sqrt(long_run_variance / n)``."""
    m, _ = _centred(x)
    lrv = long_run_variance(x, lag)
    if not lrv > 0.0:
        raise HacError("a constant series has no t statistic")
    return m / math.sqrt(lrv / len(x))


def ordinary_t(x: Sequence[float]) -> float:
    """``mean / (s / sqrt(n))`` with the sample standard deviation ``s`` (``n - 1``)."""
    m, d = _centred(x)
    s2 = math.fsum(v * v for v in d) / (len(d) - 1)
    if not s2 > 0.0:
        raise HacError("a constant series has no t statistic")
    return m / math.sqrt(s2 / len(d))
