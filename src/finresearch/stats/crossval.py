"""Cross-validation for labels that span time: purging and an embargo.

Observation ``i`` has a label that starts at ``starts[i]`` and is only known at
``ends[i]`` (a forward return over the horizon). Observations are in time order.
A test set is a union of contiguous *groups*; for each test group ``g`` with span
``[S_g, E_g]`` (earliest start, latest end of its labels), a training
observation is dropped when

- **purged**: its label interval meets the span, ``starts[i] <= E_g`` and
  ``ends[i] >= S_g``, so its label shares outcomes with a test label; or
- **embargoed**: it starts within ``embargo`` after the span,
  ``E_g < starts[i] <= E_g + embargo``. Returns are serially correlated, so
  what comes right after the test labels still carries information about them.
  Before a test group purging is enough (López de Prado 2018, chapter 7).

``purged_kfold`` uses each of ``folds`` groups as the test set once.
``combinatorial_purged`` (chapter 12) uses every combination of ``test_groups``
of ``groups`` groups; each group is then tested in ``C(groups - 1, test_groups -
1)`` splits, which is the number of complete backtest paths ``backtest_paths``
assembles: path ``p`` takes, for each group, the ``p``-th split that tests it.

The embargo length follows the hypothesis card's rule (decision 0012): at least
the forecast horizon and not less than one day; the caller passes it.
"""

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

_MIN_GROUPS = 2


class CrossValidationError(ValueError):
    """The observations or the split parameters are not valid."""


@dataclass(frozen=True)
class Split:
    train: tuple[int, ...]
    test: tuple[int, ...]
    test_groups: tuple[int, ...]


def _check_times(starts: Sequence[datetime], ends: Sequence[datetime]) -> None:
    if len(starts) != len(ends):
        raise CrossValidationError(f"{len(starts)} starts but {len(ends)} ends")
    for i, (s, e) in enumerate(zip(starts, ends, strict=True)):
        for name, t in (("start", s), ("end", e)):
            if t.tzinfo is None or t.utcoffset() != timedelta(0):
                raise CrossValidationError(f"{name} {i} must be in UTC, got {t!r}")
        if e < s:
            raise CrossValidationError(f"observation {i} ends before it starts")
        if i and s < starts[i - 1]:
            raise CrossValidationError(f"observations must be in time order (at {i})")


def group_bounds(n: int, groups: int) -> list[tuple[int, int]]:
    """``groups`` contiguous index ranges ``[lo, hi)`` covering ``0..n-1``; the first ``n % groups``
    ranges have one more observation."""
    if not _MIN_GROUPS <= groups <= n:
        raise CrossValidationError(f"need {_MIN_GROUPS} <= groups <= n = {n}, got {groups}")
    size, extra = divmod(n, groups)
    bounds: list[tuple[int, int]] = []
    lo = 0
    for g in range(groups):
        hi = lo + size + (1 if g < extra else 0)
        bounds.append((lo, hi))
        lo = hi
    return bounds


def _split(
    starts: Sequence[datetime],
    ends: Sequence[datetime],
    bounds: list[tuple[int, int]],
    test_groups: tuple[int, ...],
    embargo: timedelta,
) -> Split:
    test = [i for g in test_groups for i in range(*bounds[g])]
    spans = [
        (min(starts[i] for i in range(*bounds[g])), max(ends[i] for i in range(*bounds[g])))
        for g in test_groups
    ]
    in_test = set(test)
    train = [
        i
        for i in range(len(starts))
        if i not in in_test
        and not any(
            (starts[i] <= hi and ends[i] >= lo) or (hi < starts[i] <= hi + embargo)
            for lo, hi in spans
        )
    ]
    return Split(tuple(train), tuple(test), test_groups)


def purged_kfold(
    starts: Sequence[datetime], ends: Sequence[datetime], folds: int, embargo: timedelta
) -> list[Split]:
    """K-fold over contiguous groups, each training set purged and embargoed."""
    return combinatorial_purged(starts, ends, folds, 1, embargo)


def combinatorial_purged(
    starts: Sequence[datetime],
    ends: Sequence[datetime],
    groups: int,
    test_groups: int,
    embargo: timedelta,
) -> list[Split]:
    """Every combination of ``test_groups`` of ``groups`` groups as a test set, in lexical order."""
    _check_times(starts, ends)
    if embargo < timedelta(0):
        raise CrossValidationError(f"embargo must not be negative, got {embargo}")
    bounds = group_bounds(len(starts), groups)
    if not 1 <= test_groups < groups:
        raise CrossValidationError(f"need 1 <= test_groups < groups = {groups}, got {test_groups}")
    return [
        _split(starts, ends, bounds, combo, embargo)
        for combo in itertools.combinations(range(groups), test_groups)
    ]


def path_count(groups: int, test_groups: int) -> int:
    """Complete backtest paths: ``C(groups, test_groups) * test_groups / groups``."""
    if not 1 <= test_groups < groups:
        raise CrossValidationError(f"need 1 <= test_groups < groups = {groups}, got {test_groups}")
    return math.comb(groups - 1, test_groups - 1)


def backtest_paths(splits: Sequence[Split], groups: int) -> list[list[int]]:
    """For each path, the index of the split that supplies each group's test predictions."""
    tested_by: list[list[int]] = [[] for _ in range(groups)]
    for k, split in enumerate(splits):
        for g in split.test_groups:
            tested_by[g].append(k)
    counts = {len(ks) for ks in tested_by}
    if len(counts) != 1:
        raise CrossValidationError("each group must be tested the same number of times")
    (count,) = counts
    return [[tested_by[g][p] for g in range(groups)] for p in range(count)]
