"""The probability of backtest overfitting (roadmap step 3.7).

When a researcher tries ``N`` configurations and keeps the best, the best
in-sample result is partly luck. Combinatorially symmetric cross-validation
(CSCV; Bailey, Borwein, López de Prado & Zhu 2017, Algorithm 2.3) asks how often
the in-sample winner lands in the bottom half out of sample:

1. Lay the ``N`` trials' P&L series side by side (``T`` observations each) and
   cut the time axis into ``S`` (even) consecutive pieces of ``T / S`` rows.
2. For every way of choosing ``S / 2`` pieces (``C(S, S/2)`` combinations;
   12,870 for ``S = 16``), the chosen pieces in their order are the training
   set and the rest the test set.
3. Pick the trial with the best metric on the training set; find its rank
   ``r`` among all ``N`` on the test set (1 the worst, ``N`` the best); its
   relative rank is ``w = r / (N + 1)`` and its logit ``ln(w / (1 - w))``.
4. PBO is the share of combinations whose logit is at most 0 (section 3.1:
   the logits' distribution integrated up to 0, the mass at 0 included).

With no skill anywhere, the winner's test rank is uniform on ``1 .. N``, so PBO
is ``ceil(N / 2) / N``: exactly 1/2 for even ``N``. (Definition 2.2 of the paper
writes "rank below ``N / 2``", which would count one rank fewer; the estimator
of section 3.1 is the one implemented, and it is what the paper computes.)

Ties are broken against the trial: on the training set the first of equal
winners is taken, and on the test set a tie with the winner ranks it lowest.
The cut needs ``T`` to be a multiple of ``S``: trim the series before calling.

The metric is the mean or the Sharpe ratio (mean over sample standard
deviation) of each piece of P&L. Both are pooled from per-piece counts, means
and squared deviations (Chan, Golub & LeVeque's parallel update), so a
combination costs ``S`` merges per trial rather than a pass over the data.
"""

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

_MIN_TRIALS = 2


class OverfittingError(ValueError):
    """The trials cannot be cross-validated as asked."""


class Metric(StrEnum):
    MEAN = "mean"
    SHARPE = "sharpe"


@dataclass(frozen=True)
class _Moments:
    n: int
    mean: float
    m2: float  # sum of squared deviations from the mean

    def merge(self, other: _Moments) -> _Moments:
        n = self.n + other.n
        delta = other.mean - self.mean
        return _Moments(
            n,
            self.mean + delta * other.n / n,
            self.m2 + other.m2 + delta * delta * self.n * other.n / n,
        )


def _moments(xs: Sequence[float]) -> _Moments:
    m = math.fsum(xs) / len(xs)
    return _Moments(len(xs), m, math.fsum((x - m) ** 2 for x in xs))


def _value(m: _Moments, metric: Metric) -> float:
    if metric is Metric.MEAN:
        return m.mean
    variance = m.m2 / (m.n - 1)
    if not variance > 0.0:
        raise OverfittingError("a trial is constant over half of the sample: no Sharpe ratio")
    return m.mean / math.sqrt(variance)


@dataclass(frozen=True)
class Overfitting:
    pbo: float
    logits: tuple[float, ...]  # one per combination
    selected: tuple[tuple[float, float], ...]  # the winner's (training, test) metric, likewise

    @property
    def combinations(self) -> int:
        return len(self.logits)

    @property
    def probability_of_loss(self) -> float:
        """Share of combinations in which the training winner loses on the test set (3.2)."""
        return sum(1 for _, test in self.selected if test < 0.0) / len(self.selected)


def relative_rank(test: Sequence[float], winner: int) -> float:
    """``r / (N + 1)``: the winner's rank on the test set, ties counted against it."""
    below = sum(1 for v in test if v < test[winner])
    return (below + 1) / (len(test) + 1)


def logit(w: float) -> float:
    return math.log(w / (1.0 - w))


def probability_of_overfitting(
    trials: Sequence[Sequence[float]], partitions: int, metric: Metric = Metric.SHARPE
) -> Overfitting:
    """CSCV over ``trials`` (one P&L series per configuration) cut into ``partitions`` pieces."""
    if len(trials) < _MIN_TRIALS:
        raise OverfittingError(f"need at least {_MIN_TRIALS} trials, got {len(trials)}")
    t = len(trials[0])
    if any(len(series) != t for series in trials):
        raise OverfittingError("every trial needs the same number of observations")
    if partitions < 2 or partitions % 2:  # noqa: PLR2004 - "even and at least 2" is the rule
        raise OverfittingError(f"partitions must be even and at least 2, got {partitions}")
    if t % partitions or t // partitions < 1:
        raise OverfittingError(f"{t} observations do not cut into {partitions} equal pieces")
    if not all(math.isfinite(v) for series in trials for v in series):
        raise OverfittingError("P&L must be finite")
    size = t // partitions
    pieces = [
        [_moments(series[p * size : (p + 1) * size]) for p in range(partitions)]
        for series in trials
    ]

    def pooled(per_piece: list[_Moments], chosen: Sequence[int]) -> float:
        total = per_piece[chosen[0]]
        for p in chosen[1:]:
            total = total.merge(per_piece[p])
        return _value(total, metric)

    logits: list[float] = []
    selected: list[tuple[float, float]] = []
    everything = range(partitions)
    for train in itertools.combinations(everything, partitions // 2):
        test = [p for p in everything if p not in train]
        train_values = [pooled(per_piece, train) for per_piece in pieces]
        test_values = [pooled(per_piece, test) for per_piece in pieces]
        winner = train_values.index(max(train_values))
        logits.append(logit(relative_rank(test_values, winner)))
        selected.append((train_values[winner], test_values[winner]))
    pbo = sum(1 for v in logits if v <= 0.0) / len(logits)
    return Overfitting(pbo, tuple(logits), tuple(selected))
