from dataclasses import replace
from datetime import date

import pytest
from pydantic import ValidationError

from finresearch.registry.models import DateRange, Plan
from finresearch.verdict import (
    Check,
    Evidence,
    Outcome,
    Thresholds,
    VerdictError,
    judge,
)

DEFAULTS = Thresholds(t=3.0, dsr_min=0.95, pbo_max=0.10)

# Clears every check at the default thresholds
GOOD = Evidence(
    t=3.4,
    dsr=0.97,
    segments=[0.2, 0.1, -0.05, 0.3],
    pbo=0.04,
    cost_doubled=0.5,
    without_best_two=0.3,
    perturbed=[0.8, 1.1, 0.9, 1.0],
)


def plan(**thresholds: float) -> Plan:
    return Plan(
        confirmation=DateRange(start=date(2027, 1, 1), end=date(2027, 12, 31)),
        vault=DateRange(start=date(2028, 1, 1), end=date(2028, 6, 30)),
        metric="m",
        test="t",
        **thresholds,  # type: ignore[arg-type]
    )


def test_defaults_come_from_the_protocol_config() -> None:
    assert Thresholds.defaults() == DEFAULTS


def test_a_card_hands_over_its_frozen_thresholds() -> None:
    assert Thresholds.from_plan(plan()) == DEFAULTS
    stricter = plan(threshold_t=3.5, dsr_min=0.97, pbo_max=0.05)
    assert Thresholds.from_plan(stricter) == Thresholds(3.5, 0.97, 0.05)
    with pytest.raises(ValidationError):
        plan(dsr_min=0.90)  # the card itself refuses a looser bar


def test_clean_evidence_passes_every_check() -> None:
    verdict = judge(GOOD, DEFAULTS)
    assert verdict.passed
    assert verdict.failed == ()
    assert set(verdict.outcomes) == set(Check)
    assert all(o is Outcome.PASSED for o in verdict.outcomes.values())


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"t": 2.99}, Check.T_STAT),
        ({"dsr": 0.94}, Check.DSR),
        ({"pbo": 0.11}, Check.PBO),
        ({"cost_doubled": -0.1}, Check.COST_DOUBLED),
        ({"without_best_two": 0.0}, Check.WITHOUT_BEST_TWO),
        ({"perturbed": [0.8, 1.1, -0.01, 1.0]}, Check.PERTURBATION),
        ({"segments": [0.2, -0.1, -0.05, 0.3]}, Check.SEGMENTS),  # 2 of 4 is not a majority
    ],
)
def test_each_check_fails_on_its_own(change: dict[str, object], check: Check) -> None:
    verdict = judge(replace(GOOD, **change), DEFAULTS)  # type: ignore[arg-type]
    assert not verdict.passed
    assert verdict.failed == (check,)


def test_thresholds_are_inclusive_and_signs_strict() -> None:
    edge = replace(GOOD, t=3.0, dsr=0.95, pbo=0.10, segments=[0.1, 0.2, -0.3, 0.4, -0.5])
    assert judge(edge, DEFAULTS).passed  # t = 3, DSR = 0.95, PBO = 0.10 and 3 of 5 all pass
    assert judge(replace(GOOD, cost_doubled=0.0), DEFAULTS).failed == (Check.COST_DOUBLED,)


def test_checks_that_do_not_apply_are_reported_not_passed() -> None:
    bare = Evidence(t=3.4, dsr=0.97, segments=[0.1, 0.2, 0.3])
    verdict = judge(bare, DEFAULTS)
    assert verdict.passed
    for check in (Check.PBO, Check.COST_DOUBLED, Check.WITHOUT_BEST_TWO, Check.PERTURBATION):
        assert verdict.outcomes[check] is Outcome.NOT_APPLICABLE
    for check in (Check.T_STAT, Check.DSR, Check.SEGMENTS):
        assert verdict.outcomes[check] is Outcome.PASSED


def test_a_stricter_card_gives_a_stricter_verdict() -> None:
    borderline = replace(GOOD, t=3.2, dsr=0.96, pbo=0.08)
    assert judge(borderline, DEFAULTS).passed
    card = Thresholds.from_plan(plan(threshold_t=3.5, dsr_min=0.97, pbo_max=0.05))
    assert judge(borderline, card).failed == (Check.T_STAT, Check.DSR, Check.PBO)


def test_the_meta_research_may_ask_about_any_bar() -> None:
    weak = replace(GOOD, t=2.1)
    assert not judge(weak, DEFAULTS).passed
    assert judge(weak, Thresholds(t=2.0, dsr_min=0.95, pbo_max=0.10)).passed


def test_unusable_inputs_are_refused() -> None:
    bad_evidence = [
        {"segments": [0.1]},
        {"cost_doubled": None},  # without_best_two still given
        {"dsr": 1.2},
        {"pbo": -0.1},
    ]
    for change in bad_evidence:
        with pytest.raises(VerdictError):
            replace(GOOD, **change)  # type: ignore[arg-type]
    for t, dsr, pbo in [(0.0, 0.95, 0.1), (3.0, 1.0, 0.1), (3.0, 0.95, 0.0)]:
        with pytest.raises(VerdictError):
            Thresholds(t, dsr, pbo)
