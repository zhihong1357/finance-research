"""Properties of the Phase 3 statistics and the verdict that must hold for any valid input."""

import math
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from finresearch.sim.capacity import Strategy, capacity, net_result
from finresearch.stats.crossval import combinatorial_purged, group_bounds, path_count, purged_kfold
from finresearch.stats.montecarlo import proportion, wilson_interval
from finresearch.stats.power import (
    required_per_group,
    required_sharpe_observations,
    sharpe_power,
    two_sample_power,
)
from finresearch.stats.sharpe import deflated_sharpe, probabilistic_sharpe
from finresearch.verdict import Evidence, Outcome, Thresholds, judge

T0 = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


# ---------------------------------------------------------------- cross-validation
@st.composite
def labels(draw: st.DrawFn) -> tuple[list[datetime], list[datetime]]:
    n = draw(st.integers(min_value=4, max_value=40))
    gaps = draw(st.lists(st.integers(min_value=0, max_value=3), min_size=n, max_size=n))
    starts, t = [], T0
    for g in gaps:
        t += g * DAY
        starts.append(t)
    ends = [s + draw(st.integers(min_value=0, max_value=5)) * DAY for s in starts]
    return starts, ends


@settings(max_examples=200)
@given(labels(), st.data())
def test_no_training_label_overlaps_or_closely_follows_a_test_group(
    times: tuple[list[datetime], list[datetime]], data: st.DataObject
) -> None:
    starts, ends = times
    n = len(starts)
    groups = data.draw(st.integers(min_value=2, max_value=min(6, n)))
    k = data.draw(st.integers(min_value=1, max_value=groups - 1))
    embargo = data.draw(st.integers(min_value=0, max_value=4)) * DAY
    bounds = group_bounds(n, groups)
    splits = combinatorial_purged(starts, ends, groups, k, embargo)
    assert len(splits) == math.comb(groups, k)
    for split in splits:
        assert not set(split.train) & set(split.test)
        assert sorted(split.test) == sorted(i for g in split.test_groups for i in range(*bounds[g]))
        for g in split.test_groups:
            lo, hi = bounds[g]
            span_start, span_end = min(starts[lo:hi]), max(ends[lo:hi])
            for i in split.train:
                assert ends[i] < span_start or starts[i] > span_end + embargo
    for g in range(groups):  # every group is tested C(groups - 1, k - 1) times
        assert sum(g in s.test_groups for s in splits) == path_count(groups, k)


@settings(max_examples=100)
@given(labels(), st.integers(min_value=2, max_value=4))
def test_a_longer_embargo_never_adds_training_data(
    times: tuple[list[datetime], list[datetime]], folds: int
) -> None:
    starts, ends = times
    short = purged_kfold(starts, ends, folds, DAY)
    long = purged_kfold(starts, ends, folds, 3 * DAY)
    for a, b in zip(short, long, strict=True):
        assert set(b.train) <= set(a.train)
        assert a.test == b.test


# ---------------------------------------------------------------- Sharpe and power
srs = st.floats(min_value=0.01, max_value=0.5)
skews = st.floats(min_value=-1.0, max_value=1.0)


@settings(max_examples=200)
@given(srs, st.integers(min_value=10, max_value=5000), skews)
def test_probabilistic_sharpe_is_a_probability_that_grows_with_data(
    sr: float, n: int, skew: float
) -> None:
    kurt = 3.0 + skew * skew  # kurtosis >= 1 + skew^2 holds for any distribution
    p = probabilistic_sharpe(sr, 0.0, n, skew, kurt)
    assert 0.5 <= p <= 1.0
    assert probabilistic_sharpe(sr, 0.0, 2 * n, skew, kurt) >= p


@settings(max_examples=200)
@given(srs, st.integers(min_value=50, max_value=5000), st.floats(min_value=2.0, max_value=1e4))
def test_deflation_grows_with_the_number_of_trials(sr: float, n: int, trials: float) -> None:
    variance = 0.001
    assert deflated_sharpe(sr, n, 0.0, 3.0, 2 * trials, variance) <= deflated_sharpe(
        sr, n, 0.0, 3.0, trials, variance
    )


@settings(max_examples=200)
@given(srs, st.floats(min_value=1.0, max_value=4.0), st.floats(min_value=0.5, max_value=0.95))
def test_the_required_length_delivers_the_power_asked_for(
    sr: float, threshold: float, power: float
) -> None:
    n = required_sharpe_observations(sr, threshold, power)
    assert sharpe_power(sr, n, threshold, two_sided=False) >= power - 1e-9
    if n > 3:  # one observation fewer falls short (rounding up, not padding)
        assert sharpe_power(sr, n - 1, threshold, two_sided=False) < power + 1e-9


@settings(max_examples=200)
@given(
    st.floats(min_value=0.05, max_value=2.0),
    st.floats(min_value=1.0, max_value=4.0),
    st.floats(min_value=0.5, max_value=0.95),
)
def test_the_group_size_delivers_the_power_asked_for(
    d: float, threshold: float, power: float
) -> None:
    n = required_per_group(d, threshold, power)
    assert two_sample_power(d, n, threshold, two_sided=False) >= power - 1e-9


# ---------------------------------------------------------------- Monte Carlo
@settings(max_examples=300)
@given(st.integers(min_value=1, max_value=100_000), st.data())
def test_wilson_interval_brackets_the_share_and_widens_with_confidence(
    n: int, data: st.DataObject
) -> None:
    hits = data.draw(st.integers(min_value=0, max_value=n))
    est = proportion(hits, n)
    narrow, wide = wilson_interval(hits, n, 0.9), wilson_interval(hits, n, 0.99)
    assert wide[0] <= narrow[0] <= est.value <= narrow[1] <= wide[1]
    assert est.standard_error <= 0.5 / math.sqrt(n) + 1e-15


# ---------------------------------------------------------------- the verdict
@settings(max_examples=300)
@given(
    st.floats(min_value=0.0, max_value=6.0),
    st.floats(min_value=0.0, max_value=1.0),
    st.floats(min_value=0.0, max_value=1.0),
    st.lists(st.floats(min_value=-1.0, max_value=1.0), min_size=2, max_size=8),
    st.floats(min_value=0.0, max_value=1.0),
)
def test_a_stricter_bar_never_turns_a_failure_into_a_pass(
    t: float, dsr: float, pbo: float, segments: list[float], tighten: float
) -> None:
    evidence = Evidence(t=t, dsr=dsr, segments=segments, pbo=pbo)
    loose = Thresholds(t=3.0, dsr_min=0.95, pbo_max=0.10)
    strict = Thresholds(
        t=3.0 + tighten, dsr_min=0.95 + 0.04 * tighten, pbo_max=0.10 - 0.09 * tighten
    )
    a, b = judge(evidence, loose), judge(evidence, strict)
    for check, outcome in a.outcomes.items():
        if outcome is Outcome.FAILED:
            assert b.outcomes[check] is Outcome.FAILED
    assert not (b.passed and not a.passed)


# ---------------------------------------------------------------- capacity
@settings(max_examples=200)
@given(
    st.floats(min_value=0.002, max_value=0.05),
    st.floats(min_value=1.0, max_value=50.0),
    st.floats(min_value=0.0, max_value=0.0005),
    st.floats(min_value=1e6, max_value=1e10),
)
def test_the_best_size_beats_every_other(
    edge: float, trades: float, linear: float, volume: float
) -> None:
    s = Strategy(edge, trades, 1.0, linear, 1.0, 0.04, volume)
    cap = capacity(s)
    if s.a <= 0.0:
        assert cap.best == 0.0
        return
    assert math.isclose(
        net_result(s, cap.break_even), 0.0, abs_tol=1e-6 * max(1.0, s.a * cap.break_even)
    )
    for factor in (0.5, 0.9, 1.1, 2.0):
        assert net_result(s, cap.best * factor) <= cap.best_net + 1e-9 * max(1.0, cap.best_net)
