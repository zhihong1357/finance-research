import math
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest

from finresearch.core.time import utc
from finresearch.data.pit import Clock, LookaheadError, PointInTime
from finresearch.model.signal import Signal, SignalError, SignalSpec, evaluate, standardize
from finresearch.registry.models import Universe
from tests.data.conftest import make_table

SPEC = SignalSpec(
    name="latest_level",
    version=1,
    hypothesis="H-0001",
    universe=Universe.SYNTHETIC,
    horizon=timedelta(days=30),
    sources=("macro",),
)


def latest_level(pit: PointInTime, config: Mapping[str, Any]) -> dict[str, float]:
    """The most recent observation of each series, as known at the door's limit."""
    latest: dict[str, tuple[Any, float]] = {}
    for row in pit.latest("macro").rows():
        seen = latest.get(row["series"])
        if seen is None or row["event_time"] > seen[0]:
            latest[row["series"]] = (row["event_time"], row["value"])
    return {k: v * config.get("scale", 1.0) for k, (_, v) in latest.items()}


def peeks_ahead(pit: PointInTime, config: Mapping[str, Any]) -> dict[str, float]:
    """A signal that tries to read tomorrow."""
    snap = pit.as_of("macro", pit.clock.limit + timedelta(days=1))
    return {row["series"]: row["value"] for row in snap.rows()}


def door() -> PointInTime:
    return PointInTime(Clock(limit=utc(2026, 12, 1)), [make_table()])


def test_a_signal_sees_only_what_was_public_at_the_decision() -> None:
    # The table: CPI for September first published 3.0 on 15 Oct, revised to 3.1 on 13 Nov,
    # when October's 2.9 also came out; PPI for September 1.0 on 16 Oct.
    signal = Signal(SPEC, latest_level)
    before = evaluate(signal, door(), utc(2026, 11, 1), {})
    assert before.values == {"CPI": 3.0, "PPI": 1.0}  # the revision is not visible yet
    assert before.at == utc(2026, 11, 1)
    assert before.signal == "latest_level@v1"
    assert before.horizon == timedelta(days=30)
    after = evaluate(signal, door(), utc(2026, 11, 14), {"scale": 2.0})
    assert after.values == {"CPI": 5.8, "PPI": 2.0}  # October's 2.9 is now the latest CPI


def test_reading_past_the_decision_instant_is_refused() -> None:
    with pytest.raises(LookaheadError):
        evaluate(Signal(SPEC, peeks_ahead), door(), utc(2026, 11, 1), {})


def test_a_decision_past_the_door_is_refused() -> None:
    with pytest.raises(LookaheadError):
        evaluate(Signal(SPEC, latest_level), door(), utc(2026, 12, 2), {})


def test_reads_are_recorded_on_the_outer_door() -> None:
    d = door()
    evaluate(Signal(SPEC, latest_level), d, utc(2026, 11, 1), {})
    assert list(d.used) == ["macro"]


def test_outputs_are_checked() -> None:
    for bad in ({"CPI": math.nan}, {"CPI": math.inf}, {"": 1.0}):
        signal = Signal(SPEC, lambda pit, config, out=bad: out)
        with pytest.raises(SignalError):
            evaluate(signal, door(), utc(2026, 11, 1), {})
    empty = evaluate(Signal(SPEC, lambda pit, config: {}), door(), utc(2026, 11, 1), {})
    assert empty.values == {}  # no opinion is a valid output


def test_scores_are_sorted_by_instrument() -> None:
    out = {"ZEC": 1.0, "BTC": -1.0, "ETH": 0.5}
    scores = evaluate(Signal(SPEC, lambda pit, config: out), door(), utc(2026, 11, 1), {})
    assert list(scores.values) == ["BTC", "ETH", "ZEC"]


def test_standardize_by_hand() -> None:
    # 1, 2, 3: mean 2, population sd sqrt(2/3) = 0.816497; z = -1.224745, 0, 1.224745
    z = standardize({"a": 1.0, "b": 2.0, "c": 3.0})
    assert z == {
        "a": pytest.approx(-1.224745, abs=1e-6),
        "b": 0.0,
        "c": pytest.approx(1.224745, abs=1e-6),
    }
    assert standardize({"a": 5.0}) == {"a": 0.0}
    assert standardize({"a": 2.0, "b": 2.0}) == {"a": 0.0, "b": 0.0}
    assert standardize({}) == {}
    with pytest.raises(SignalError):
        standardize({"a": math.nan, "b": 1.0})


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "has space"},
        {"version": 0},
        {"hypothesis": "H-1"},
        {"horizon": timedelta(0)},
        {"sources": ()},
    ],
)
def test_bad_declarations_are_refused(change: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "name": SPEC.name,
        "version": SPEC.version,
        "hypothesis": SPEC.hypothesis,
        "universe": SPEC.universe,
        "horizon": SPEC.horizon,
        "sources": SPEC.sources,
    } | change
    with pytest.raises(SignalError):
        SignalSpec(**fields)
