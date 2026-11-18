"""Tests that know how many strategies were tried (roadmap step 3.4).

When the best of ``K`` strategies is tested as if it were the only one, chance
alone makes it look good. Both tests here resample all ``K`` strategies'
performance series together with one set of stationary-bootstrap blocks
(``stats.bootstrap``), so the dependence across strategies and over time is
kept.

- **Reality Check** (White 2000, section 2.3 and pp. 1109-1110): is the best
  strategy better than the benchmark at all? ``V = max_k sqrt(n) mean_k``;
  each resample gives ``V*_i = max_k sqrt(n) (mean*_k,i - mean_k)``; with
  ``M`` the number of ``V*`` at or below ``V`` among ``N`` resamples, the
  p-value is ``1 - M / N``.
- **StepM** (Romano & Wolf 2005; Algorithms 3.1 and 3.2 of the 2003 working
  paper, basic statistics): which strategies beat the benchmark, controlling
  the familywise error rate at ``alpha``. With ``w_k`` the mean performance, the
  first step rejects every ``k`` with ``w_k > c``, where ``c`` is the ``1 - alpha``
  quantile of ``max_k (w*_k - w_k)`` over the resamples; each further step
  recomputes ``c`` over the strategies not yet rejected, until a step rejects
  nothing. The quantile is the paper's (equation 10): the smallest value whose
  empirical distribution function reaches ``1 - alpha``.

``performance[k]`` is strategy ``k``'s performance relative to the benchmark
at each time (for strategies judged against zero, simply its returns). Both
tests use the basic statistic, the mean, so a volatile strategy weighs more:
scale the series to a common volatility first when that is not wanted. The
studentized StepM (the paper's section 4) and Hansen's (2005) SPA test are not
implemented: the first needs a kernel variance estimate inside every resample,
and the second paper has not been read yet.

The bootstrap draws are separated from the decisions (``reality_check_p_value``
and ``stepdown`` take them as numbers), so every decision rule can be checked
by hand.
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from finresearch.stats.bootstrap import stationary_blocks

_MIN_STRATEGIES = 1
_SLACK = 1e-9  # ceil((1 - alpha) J) must not round up past an exact integer


class SnoopingError(ValueError):
    """The strategies or the draws cannot be tested as asked."""


@dataclass(frozen=True)
class RealityCheck:
    statistic: float  # V = max_k sqrt(n) mean_k
    p_value: float
    best: int  # the strategy with the largest mean


@dataclass(frozen=True)
class StepdownResult:
    rejected: tuple[int, ...]  # strategies that beat the benchmark, in the order rejected
    steps: tuple[tuple[int, ...], ...]  # which were rejected at each step
    critical_values: tuple[float, ...]  # c at each step (the last one rejected nothing)


def _check(performance: Sequence[Sequence[float]]) -> int:
    if len(performance) < _MIN_STRATEGIES:
        raise SnoopingError("need at least one strategy")
    n = len(performance[0])
    if n < 2 or any(len(s) != n for s in performance):  # noqa: PLR2004 - a mean and its spread
        raise SnoopingError("every strategy needs the same number (at least 2) of observations")
    if not all(math.isfinite(v) for s in performance for v in s):
        raise SnoopingError("performance must be finite")
    return n


def resampled_means(
    performance: Sequence[Sequence[float]],
    mean_block: float,
    replications: int,
    rng: random.Random,
) -> tuple[list[float], list[list[float]]]:
    """The strategies' means, and ``replications`` resamples of them with shared blocks.

    Returns ``(means, draws)`` with ``draws[j][k]`` strategy ``k``'s mean in resample ``j``.
    """
    n = _check(performance)
    if replications < 1:
        raise SnoopingError(f"need at least one replication, got {replications}")
    means = [math.fsum(s) / n for s in performance]
    prefixes: list[list[float]] = []
    for s, m in zip(performance, means, strict=True):
        prefix = [0.0]
        for v in s:  # centred, so the running totals stay small
            prefix.append(prefix[-1] + (v - m))
        prefixes.append(prefix)
    draws: list[list[float]] = []
    for _ in range(replications):
        blocks = stationary_blocks(n, mean_block, rng)
        row: list[float] = []
        for prefix, m in zip(prefixes, means, strict=True):
            total = prefix[n]
            sums: list[float] = []
            for start, length in blocks:
                end = start + length
                if end <= n:
                    sums.append(prefix[end] - prefix[start])
                else:
                    sums.append(total - prefix[start] + prefix[end - n])
            row.append(m + math.fsum(sums) / n)
        draws.append(row)
    return means, draws


def reality_check_p_value(
    means: Sequence[float], draws: Sequence[Sequence[float]], n: int
) -> RealityCheck:
    """White's p-value from the means and the resampled means (``n`` observations each)."""
    if not draws or any(len(row) != len(means) for row in draws):
        raise SnoopingError("every draw needs one mean per strategy")
    root = math.sqrt(n)
    statistic = root * max(means)
    v_star = [root * max(d - m for d, m in zip(row, means, strict=True)) for row in draws]
    at_or_below = sum(1 for v in v_star if v <= statistic)
    return RealityCheck(statistic, 1.0 - at_or_below / len(v_star), means.index(max(means)))


def _quantile(values: Sequence[float], level: float) -> float:
    """``inf{x : F(x) >= level}`` for the empirical distribution of ``values``."""
    s = sorted(values)
    return s[max(0, math.ceil(level * len(s) - _SLACK) - 1)]


def stepdown(
    means: Sequence[float], draws: Sequence[Sequence[float]], alpha: float
) -> StepdownResult:
    """Romano & Wolf's StepM decisions from the means and the resampled means."""
    if not 0.0 < alpha < 1.0:
        raise SnoopingError(f"alpha must be strictly between 0 and 1, got {alpha}")
    if not draws or any(len(row) != len(means) for row in draws):
        raise SnoopingError("every draw needs one mean per strategy")
    order = sorted(range(len(means)), key=lambda k: (-means[k], k))
    centred = [[d - m for d, m in zip(row, means, strict=True)] for row in draws]
    remaining: list[int] = list(order)
    rejected: list[int] = []
    steps: list[tuple[int, ...]] = []
    criticals: list[float] = []
    while remaining:
        c = _quantile([max(row[k] for k in remaining) for row in centred], 1.0 - alpha)
        criticals.append(c)
        now = tuple(k for k in remaining if means[k] - c > 0.0)
        if not now:
            break
        steps.append(now)
        rejected.extend(now)
        remaining = [k for k in remaining if k not in now]
    return StepdownResult(tuple(rejected), tuple(steps), tuple(criticals))


def reality_check(
    performance: Sequence[Sequence[float]],
    mean_block: float,
    replications: int,
    rng: random.Random,
) -> RealityCheck:
    """White's Bootstrap Reality Check: does the best strategy beat the benchmark?"""
    means, draws = resampled_means(performance, mean_block, replications, rng)
    return reality_check_p_value(means, draws, len(performance[0]))


def stepm(
    performance: Sequence[Sequence[float]],
    alpha: float,
    mean_block: float,
    replications: int,
    rng: random.Random,
) -> StepdownResult:
    """Romano & Wolf's StepM: which strategies beat the benchmark (familywise error ``alpha``)."""
    means, draws = resampled_means(performance, mean_block, replications, rng)
    return stepdown(means, draws, alpha)
