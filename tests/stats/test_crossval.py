import bisect
import math
import random
from datetime import UTC, datetime, timedelta, timezone

import pytest

from finresearch.stats.crossval import (
    CrossValidationError,
    Split,
    backtest_paths,
    combinatorial_purged,
    group_bounds,
    path_count,
    purged_kfold,
)
from finresearch.stats.montecarlo import mean

T0 = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


def daily(n: int, horizon_days: int) -> tuple[list[datetime], list[datetime]]:
    starts = [T0 + i * DAY for i in range(n)]
    return starts, [s + horizon_days * DAY for s in starts]


def test_purged_kfold_by_hand() -> None:
    # 10 daily observations, each label known 2 days after it starts; 2 folds; embargo 1 day.
    starts, ends = daily(10, 2)
    first, second = purged_kfold(starts, ends, 2, DAY)
    # Fold 1 tests 0..4: span [d0, d6]. Purged: 5 (d5..d7) and 6 (d6..d8) meet it.
    # Embargoed: 7 starts at d7, within (d6, d7]. Left: 8, 9.
    assert first == Split(train=(8, 9), test=(0, 1, 2, 3, 4), test_groups=(0,))
    # Fold 2 tests 5..9: span [d5, d11]. Purged: 3 (ends d5) and 4 (ends d6). Nothing after it.
    assert second == Split(train=(0, 1, 2), test=(5, 6, 7, 8, 9), test_groups=(1,))


def test_without_overlap_or_embargo_it_is_plain_kfold() -> None:
    starts, ends = daily(9, 0)
    splits = purged_kfold(starts, ends, 3, timedelta(0))
    assert [s.test for s in splits] == [(0, 1, 2), (3, 4, 5), (6, 7, 8)]
    for s in splits:
        assert sorted(s.train + s.test) == list(range(9))


def test_group_bounds_by_hand() -> None:
    assert group_bounds(10, 3) == [(0, 4), (4, 7), (7, 10)]
    assert group_bounds(4, 4) == [(0, 1), (1, 2), (2, 3), (3, 4)]


def test_a_test_set_in_two_pieces_is_purged_on_both_sides_of_each() -> None:
    # 8 daily observations, horizon 1 day, 4 groups of 2, test groups 0 and 2, embargo 1 day.
    starts, ends = daily(8, 1)
    split = next(
        s for s in combinatorial_purged(starts, ends, 4, 2, DAY) if s.test_groups == (0, 2)
    )
    # Group 0 = 0..1, span [d0, d2]: purge 2 (starts d2); embargo 3 (starts d3, within (d2, d3]).
    # Group 2 = 4..5, span [d4, d6]: purge 3 (ends d4) and 6 (starts d6); embargo 7 (starts d7).
    # Nothing is left.
    assert split.test == (0, 1, 4, 5)
    assert split.train == ()
    # Without the embargo: 2, 3 and 6 are still purged; 7 survives.
    split = next(
        s for s in combinatorial_purged(starts, ends, 4, 2, timedelta(0)) if s.test_groups == (0, 2)
    )
    assert split.train == (7,)


def test_combinatorial_counts_and_paths() -> None:
    starts, ends = daily(60, 1)
    splits = combinatorial_purged(starts, ends, 6, 2, DAY)
    assert len(splits) == math.comb(6, 2) == 15
    assert path_count(6, 2) == 5  # C(6, 2) * 2 / 6 = C(5, 1)
    assert path_count(5, 1) == 1  # plain k-fold is one path
    assert path_count(10, 3) == math.comb(10, 3) * 3 // 10 == 36
    paths = backtest_paths(splits, 6)
    assert len(paths) == 5
    for path in paths:
        for g, k in enumerate(path):
            assert g in splits[k].test_groups
    for g in range(6):  # every split that tests group g is used by exactly one path
        assert sorted(p[g] for p in paths) == [
            k for k, s in enumerate(splits) if g in s.test_groups
        ]


def test_no_training_label_meets_or_closely_follows_a_test_group() -> None:
    starts, ends = daily(37, 3)
    embargo = 2 * DAY
    bounds = group_bounds(37, 5)
    for split in combinatorial_purged(starts, ends, 5, 2, embargo):
        assert not set(split.train) & set(split.test)
        for g in split.test_groups:
            lo, hi = bounds[g]
            span_start, span_end = starts[lo], max(ends[lo:hi])
            for i in split.train:
                assert ends[i] < span_start or starts[i] > span_end + embargo


def test_invalid_inputs_are_refused() -> None:
    starts, ends = daily(10, 1)
    naive = [datetime(2026, 1, 1) + i * DAY for i in range(10)]  # noqa: DTZ001 -- deliberately naive
    plus8 = [s.astimezone(timezone(timedelta(hours=8))) for s in starts]
    cases = [
        (naive, ends, 2, DAY),
        (starts, plus8, 2, DAY),
        (starts[::-1], ends[::-1], 2, DAY),
        (starts, starts[:5] + [s - DAY for s in starts[5:]], 2, DAY),
        (starts, ends[:-1], 2, DAY),
        (starts, ends, 1, DAY),
        (starts, ends, 11, DAY),
        (starts, ends, 2, -DAY),
    ]
    for s, e, folds, embargo in cases:
        with pytest.raises(CrossValidationError):
            purged_kfold(s, e, folds, embargo)
    with pytest.raises(CrossValidationError):
        combinatorial_purged(starts, ends, 4, 4, DAY)
    with pytest.raises(CrossValidationError):
        path_count(4, 0)
    lopsided = [Split((), (0,), (0,)), Split((), (0,), (0,))]
    with pytest.raises(CrossValidationError):
        backtest_paths(lopsided, 2)


# ---------------------------------------------------------------- leakage on overlapping labels
N, HORIZON, FOLDS, REPLICATIONS = 400, 5, 20, 200


def _nearest_label_covariance(splits: list[Split], labels: list[float]) -> float:
    """Mean over splits of the sample covariance between each test label and the label of the
    nearest training observation. When a split's training labels share no shocks with its test
    labels the two are independent and every split's covariance has expectation exactly zero.
    (A correlation pooled over splits would not: one split's training labels overlap the next
    split's test labels, which biases the pooled means.)"""
    covariances = []
    for split in splits:
        train = list(split.train)
        predicted, actual = [], []
        for i in split.test:
            k = bisect.bisect_left(train, i)
            near = train[max(0, k - 1) : k + 1]  # the training observations on either side
            j = min(near, key=lambda j: abs(j - i))
            predicted.append(labels[j])
            actual.append(labels[i])
        mp, ma = math.fsum(predicted) / len(predicted), math.fsum(actual) / len(actual)
        products = [(x - mp) * (y - ma) for x, y in zip(predicted, actual, strict=True)]
        covariances.append(math.fsum(products) / (len(products) - 1))
    return math.fsum(covariances) / len(covariances)


def test_overlapping_labels_leak_without_purging_and_not_with_it() -> None:
    # Pure noise: label i is the sum of the next HORIZON shocks, so neighbours share outcomes and
    # nothing is predictable. Naive k-fold (labels treated as instantaneous) finds "skill";
    # purging with the true label ends plus an embargo of one horizon finds none.
    # The labels overlap, so one series' observations are far from independent and a t statistic
    # on them would overstate the evidence: compare the mean over independent replications, with
    # the spread between replications as the error.
    starts = [T0 + i * DAY for i in range(N)]
    ends = [s + (HORIZON - 1) * DAY for s in starts]
    naive = purged_kfold(starts, starts, FOLDS, timedelta(0))
    purged = purged_kfold(starts, ends, FOLDS, HORIZON * DAY)
    found: dict[str, list[float]] = {"naive": [], "purged": []}
    rnd = random.Random(20261118)
    for _ in range(REPLICATIONS):
        shocks = [rnd.gauss() for _ in range(N + HORIZON)]
        labels = [math.fsum(shocks[i : i + HORIZON]) for i in range(N)]
        found["naive"].append(_nearest_label_covariance(naive, labels))
        found["purged"].append(_nearest_label_covariance(purged, labels))
    leaky, clean = mean(found["naive"]), mean(found["purged"])
    assert leaky.value - 5.0 * leaky.standard_error > 0.0
    assert abs(clean.value) < 3.0 * clean.standard_error
