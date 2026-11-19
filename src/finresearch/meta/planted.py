"""Planted effects: a signal with a known edge, buried in noise (roadmap step 3.11).

The meta-research needs data where the truth is known in both directions: the
noise factory (``meta.noise``) makes data with nothing to find, and this module
plants something of a chosen size in it, to measure what the pipeline finds.

The signal ``s_it`` is known at the end of period ``t`` and predicts the return
of period ``t + 1``:

    r'_i,t+1 = r_i,t+1 + strength x sd_i x s_it

- ``r`` is the noise and ``sd_i`` its sample standard deviation, so ``strength``
  is in units of the instrument's own volatility, whatever that is;
- ``s_it`` has mean 0 and variance 1, independent across instruments and of the
  noise, and AR(1) over time with ``persistence``:
  ``s_t = persistence x s_{t-1} + sqrt(1 - persistence^2) u_t``, ``u`` normal.
  Real signals are persistent (a funding rate, a trend), which matters for the
  tests that use them, not for estimating the strength.

``estimate_strength`` regresses the standardised next-period returns on the
signal, pooled over instruments and time (through the origin). Because the
signal is independent of the noise and the noise is unpredictable (each part is
GARCH with zero mean), the estimate is unbiased with standard error
``sqrt(sum (y - b s)^2 / (m - 1)) / sqrt(sum s^2)`` over the ``m`` pairs; for
large samples about ``sqrt(1 / m)``: the residual ``y - b s`` is the standardised noise,
whose variance is 1; the planted part is explained and does not add to it. Cross-correlation in the
noise does not inflate it, since the signal is independent across instruments.
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from finresearch.stats.montecarlo import Estimate


class PlantedError(ValueError):
    """The noise or the effect cannot be planted as asked."""


@dataclass(frozen=True)
class Planted:
    returns: tuple[tuple[float, ...], ...]  # [i][t]: noise plus the effect of signal[i][t - 1]
    signal: tuple[
        tuple[float, ...], ...
    ]  # [i][t]: known at the end of t, predicts returns[i][t + 1]
    strength: float
    scale: tuple[float, ...]  # sd_i, the noise's sample standard deviation


def signal_path(periods: int, persistence: float, rng: random.Random) -> list[float]:
    """Unit-variance AR(1), started from its stationary distribution."""
    if not -1.0 < persistence < 1.0:
        raise PlantedError(f"persistence must be strictly between -1 and 1, got {persistence}")
    innovation = math.sqrt(1.0 - persistence * persistence)
    s = rng.gauss(0.0, 1.0)
    out: list[float] = []
    for _ in range(periods):
        out.append(s)
        s = persistence * s + innovation * rng.gauss(0.0, 1.0)
    return out


def plant(
    noise: Sequence[Sequence[float]], strength: float, persistence: float, rng: random.Random
) -> Planted:
    """Add an effect of ``strength`` (volatility units) of a fresh signal to ``noise[i][t]``."""
    if not noise:
        raise PlantedError("need at least one instrument")
    t = len(noise[0])
    if t < 3 or any(len(r) != t for r in noise):  # noqa: PLR2004 - a lag and a spread
        raise PlantedError("every instrument needs the same number (at least 3) of periods")
    if not math.isfinite(strength):
        raise PlantedError(f"strength must be finite, got {strength}")
    if not all(math.isfinite(x) for r in noise for x in r):
        raise PlantedError("the noise must be finite")
    returns: list[tuple[float, ...]] = []
    signals: list[tuple[float, ...]] = []
    scales: list[float] = []
    for r in noise:
        m = math.fsum(r) / t
        sd = math.sqrt(math.fsum((x - m) ** 2 for x in r) / (t - 1))
        if not sd > 0.0:
            raise PlantedError("a constant noise series has no scale")
        s = signal_path(t, persistence, rng)
        returns.append(tuple(r[0:1]) + tuple(r[k] + strength * sd * s[k - 1] for k in range(1, t)))
        signals.append(tuple(s))
        scales.append(sd)
    return Planted(tuple(returns), tuple(signals), strength, tuple(scales))


def estimate_strength(planted: Planted) -> Estimate:
    """Pooled regression through the origin of ``returns[i][t + 1] / sd_i`` on ``signal[i][t]``."""
    xs: list[float] = []
    ys: list[float] = []
    for r, s, sd in zip(planted.returns, planted.signal, planted.scale, strict=True):
        xs.extend(s[:-1])
        ys.extend(v / sd for v in r[1:])
    sxx = math.fsum(x * x for x in xs)
    b = math.fsum(x * y for x, y in zip(xs, ys, strict=True)) / sxx
    m = len(xs)
    residual = math.fsum((y - b * x) ** 2 for x, y in zip(xs, ys, strict=True)) / (m - 1)
    return Estimate(b, math.sqrt(residual / sxx), m)
