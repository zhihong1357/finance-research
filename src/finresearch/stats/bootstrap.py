"""The stationary bootstrap and its automatic block length (roadmap step 3.3).

Returns are not independent: volatility clusters and labels overlap. Resampling
single observations (the ordinary bootstrap) then understates the uncertainty of
a mean. The **stationary bootstrap** (Politis & Romano 1994) resamples blocks of
consecutive observations instead: each block starts at a uniformly random
position and runs for a geometrically distributed number of steps (mean
``mean_block``), wrapping round from the end to the start; blocks are laid end
to end until the resample is as long as the data. A mean block of 1 is the
ordinary bootstrap.

``optimal_block_length`` chooses the mean block length automatically (Politis
& White 2004, with the correction of Patton, Politis & White 2009, which fixes
the variance constant to ``D_SB = 2 g(0)^2``). It follows the authors' published
Matlab code (``opt_block_length_REV_dec07``, Patton's code page) line for line,
because that code produced the corrected paper's tables:

1. Correlations at lags 1 to ``m_max = ceil(sqrt(n)) + K`` on the common
   sample (the last ``n - m_max`` observations against their lags), ``K = 5``.
   ``m`` is the first lag that starts a run of ``K`` correlations all below
   ``c sqrt(log10(n) / n)`` in absolute value, ``c = 2``; with no such run, the
   largest significant lag. ``M = min(2 m, m_max)``.
2. Autocovariances ``R(k)`` at lags 0 to ``M`` on the common sample (the last
   ``n - M`` observations, ``n - M - 1`` in the denominator), weighted by the
   flat-top window ``lambda(t)`` (1 up to ``|t| = 1/2``, then falling linearly to
   0 at ``|t| = 1``):
   ``G = sum_{|k|<=M} lambda(k/M) |k| R(k)``, ``g(0) = sum_{|k|<=M} lambda(k/M) R(k)``.
3. ``b_SB = (2 G^2 / D_SB)^(1/3) n^(1/3)`` with ``D_SB = 2 g(0)^2``, and for
   the circular block bootstrap ``D_CB = 4/3 g(0)^2``; both capped at
   ``ceil(min(3 sqrt(n), n / 3))``.

One addition to the published code: the result is floored at 1. Below 1 the
data look independent, and a mean block shorter than one observation means
nothing; 1 is the ordinary bootstrap.

``block_lengths`` is step 3 on its own, so the theoretical optimum can be
computed from known ``G`` and ``g(0)`` (the corrected paper's Table 1).

Randomness comes from a ``random.Random`` the caller seeds, so every resample
is reproducible (step 3.1b will move this to numpy's generators).
"""

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# The constants of the published method (Politis & White 2004; Politis 2003): not tunables.
_RUN = 5  # K: max(5, sqrt(log10 n)), and sqrt(log10 n) < 5 for any n below 10^25
_C = 2.0  # the significance bound is c sqrt(log10(n) / n)
_CB_FACTOR = 4.0 / 3.0  # D_CB = 4/3 g(0)^2
_SB_FACTOR = 2.0  # D_SB = 2 g(0)^2 (the 2009 correction)
_CAP_ROOT, _CAP_SHARE = 3.0, 3.0  # the cap: ceil(min(3 sqrt(n), n / 3))
_MIN_COMMON = 3  # a correlation needs at least this many pairs


class BootstrapError(ValueError):
    """The inputs do not support the requested resampling."""


@dataclass(frozen=True)
class BlockLengths:
    stationary: float  # mean block length for the stationary bootstrap
    circular: float  # block length for the circular block bootstrap (round up to use it)


def flat_top(t: float) -> float:
    """The flat-top lag window: 1 for ``|t| < 1/2``, ``2 (1 - |t|)`` up to ``|t| = 1``, then 0."""
    a = abs(t)
    if a < 0.5:  # noqa: PLR2004 - the window's corner, part of its definition
        return 1.0
    return 2.0 * (1.0 - a) if a <= 1.0 else 0.0


def block_lengths(g: float, g0: float, n: int) -> BlockLengths:
    """Optimal block lengths from ``G``, ``g(0)`` and the sample size (no cap, no floor).

    Only ``g(0)^2`` enters, so its sign does not matter: the flat-top window is
    not positive definite, and its estimate of ``g(0)`` can come out negative.
    """
    if g0 == 0.0 or not math.isfinite(g0) or not math.isfinite(g):
        raise BootstrapError(f"need finite G and a non-zero g(0), got {g} and {g0}")
    if n < 1:
        raise BootstrapError(f"need at least one observation, got {n}")
    scale = n ** (1.0 / 3.0)
    return BlockLengths(
        (2.0 * g * g / (_SB_FACTOR * g0 * g0)) ** (1.0 / 3.0) * scale,
        (2.0 * g * g / (_CB_FACTOR * g0 * g0)) ** (1.0 / 3.0) * scale,
    )


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    ma, mb = math.fsum(a) / len(a), math.fsum(b) / len(b)
    da, db = [x - ma for x in a], [y - mb for y in b]
    saa, sbb = math.fsum(x * x for x in da), math.fsum(y * y for y in db)
    if saa == 0.0 or sbb == 0.0:
        raise BootstrapError("a stretch of the series is constant: no correlation")
    return math.fsum(x * y for x, y in zip(da, db, strict=True)) / math.sqrt(saa * sbb)


def _covariance(a: Sequence[float], b: Sequence[float]) -> float:
    ma, mb = math.fsum(a) / len(a), math.fsum(b) / len(b)
    return math.fsum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / (len(a) - 1)


def first_quiet_lag(correlations: Sequence[float], n: int) -> int:
    """``m``: the first lag starting a run of 5 insignificant correlations (lags start at 1).

    ``correlations[j - 1]`` is the correlation at lag ``j``. With no such run,
    the largest significant lag.
    """
    bound = _C * math.sqrt(math.log10(n) / n)
    quiet = [abs(r) < bound for r in correlations]
    for j in range(len(quiet) - _RUN + 1):
        if all(quiet[j : j + _RUN]):
            return j + 1
    return max(j + 1 for j, q in enumerate(quiet) if not q)


def optimal_block_length(x: Sequence[float]) -> BlockLengths:
    """Automatic block lengths for ``x`` (Politis & White 2004, corrected 2009)."""
    xs = [float(v) for v in x]
    n = len(xs)
    if not all(math.isfinite(v) for v in xs):
        raise BootstrapError("the series must be finite")
    m_max = math.ceil(math.sqrt(n)) + _RUN
    if n - m_max < _MIN_COMMON:
        raise BootstrapError(f"{n} observations are too few (need more than {m_max + 2})")
    head = xs[m_max:]
    correlations = [_pearson(head, xs[m_max - j : n - j]) for j in range(1, m_max + 1)]
    big_m = min(2 * first_quiet_lag(correlations, n), m_max)
    tail = xs[big_m:]
    acv = [_covariance(tail, xs[big_m - k : n - k]) for k in range(big_m + 1)]
    g = 2.0 * math.fsum(flat_top(k / big_m) * k * acv[k] for k in range(1, big_m + 1))
    g0 = acv[0] + 2.0 * math.fsum(flat_top(k / big_m) * acv[k] for k in range(1, big_m + 1))
    cap = float(math.ceil(min(_CAP_ROOT * math.sqrt(n), n / _CAP_SHARE)))
    if g0 == 0.0:  # an infinite optimum, as the published code computes it: the cap
        return BlockLengths(cap, cap)
    raw = block_lengths(g, g0, n)
    return BlockLengths(
        max(1.0, min(raw.stationary, cap)),
        max(1.0, min(raw.circular, cap)),
    )


def stationary_blocks(n: int, mean_block: float, rng: random.Random) -> list[tuple[int, int]]:
    """One resample of ``n`` observations as blocks ``(start, length)``; lengths add up to ``n``.

    A block of ``length`` from ``start`` covers ``start, start + 1, ...`` modulo ``n``.
    """
    if n < 1:
        raise BootstrapError(f"need at least one observation, got {n}")
    if not (mean_block >= 1.0 and math.isfinite(mean_block)):
        raise BootstrapError(f"mean block length must be at least 1, got {mean_block}")
    randrange, uniform, log = rng.randrange, rng.random, math.log
    if mean_block == 1.0:  # the ordinary bootstrap: every block is one observation
        return [(randrange(n), 1) for _ in range(n)]
    log_stay = math.log1p(-1.0 / mean_block)
    blocks: list[tuple[int, int]] = []
    left = n
    while left > 0:
        start = randrange(n)
        # geometric on 1, 2, ...: at each step the block goes on with chance 1 - 1 / mean_block
        length = min(1 + int(log(1.0 - uniform()) / log_stay), left)
        blocks.append((start, length))
        left -= length
    return blocks


def stationary_indices(n: int, mean_block: float, rng: random.Random) -> list[int]:
    """One resample of ``n`` observations as positions into the data."""
    return [
        (start + i) % n
        for start, length in stationary_blocks(n, mean_block, rng)
        for i in range(length)
    ]


def resample_means(
    x: Sequence[float], mean_block: float, replications: int, rng: random.Random
) -> list[float]:
    """Means of ``replications`` stationary-bootstrap resamples of ``x``.

    Sums whole blocks from running totals (wrapping round the end), so a
    resample costs one step per block rather than one per observation.
    """
    xs = [float(v) for v in x]
    n = len(xs)
    if n < 1:
        raise BootstrapError("need at least one observation")
    if replications < 1:
        raise BootstrapError(f"need at least one replication, got {replications}")
    if not all(math.isfinite(v) for v in xs):
        raise BootstrapError("the series must be finite")
    if not (mean_block >= 1.0 and math.isfinite(mean_block)):
        raise BootstrapError(f"mean block length must be at least 1, got {mean_block}")
    centre = math.fsum(xs) / n
    prefix = [0.0]
    for v in xs:  # centred first, so the running totals stay small
        prefix.append(prefix[-1] + (v - centre))
    total_all = prefix[n]
    out: list[float] = []
    if mean_block == 1.0:  # the ordinary bootstrap: draw single observations directly
        centred = [v - centre for v in xs]
        return [centre + math.fsum(rng.choices(centred, k=n)) / n for _ in range(replications)]
    for _ in range(replications):
        sums: list[float] = []
        for start, length in stationary_blocks(n, mean_block, rng):
            end = start + length
            if end <= n:
                sums.append(prefix[end] - prefix[start])
            else:
                sums.append(total_all - prefix[start] + prefix[end - n])
        out.append(centre + math.fsum(sums) / n)
    return out


def resample(
    x: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
    mean_block: float,
    replications: int,
    rng: random.Random,
) -> list[float]:
    """``statistic`` on ``replications`` stationary-bootstrap resamples of ``x``."""
    xs = list(x)
    if replications < 1:
        raise BootstrapError(f"need at least one replication, got {replications}")
    return [
        statistic([xs[i] for i in stationary_indices(len(xs), mean_block, rng)])
        for _ in range(replications)
    ]


def quantile(values: Sequence[float], q: float) -> float:
    """The ``q`` quantile by linear interpolation between order statistics (``(n - 1) q``)."""
    if not values:
        raise BootstrapError("no values")
    if not 0.0 <= q <= 1.0:
        raise BootstrapError(f"q must be in [0, 1], got {q}")
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def percentile_interval(values: Sequence[float], confidence: float) -> tuple[float, float]:
    """The central ``confidence`` interval of bootstrap ``values``."""
    if not 0.0 < confidence < 1.0:
        raise BootstrapError(f"confidence must be strictly between 0 and 1, got {confidence}")
    tail = (1.0 - confidence) / 2.0
    return quantile(values, tail), quantile(values, 1.0 - tail)
