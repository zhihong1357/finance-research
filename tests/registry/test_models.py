import tomllib
from typing import Any

import pytest
from pydantic import ValidationError

from finresearch.core.fingerprint import digest_bytes
from finresearch.core.time import utc
from finresearch.registry.models import (
    HypothesisCard,
    Plan,
    Segment,
    Source,
    Status,
    TrialRecord,
)
from tests.registry.conftest import CARD, SOURCE, manifest, registered


def _card(text: str = CARD, **changes: object) -> HypothesisCard:
    raw = tomllib.loads(text)
    raw.update(changes)
    return HypothesisCard.model_validate(raw)


def _plan(**changes: object) -> Plan:
    raw = tomllib.loads(CARD)["plan"]
    raw.update(changes)
    return Plan.model_validate(raw)


def test_a_draft_card() -> None:
    card = _card()
    assert card.status is Status.DRAFT
    assert card.registered_at is None
    assert card.plan.threshold_t == 3.0  # from config/protocol.toml
    assert (card.plan.dsr_min, card.plan.pbo_max) == (0.95, 0.10)


def test_a_registered_card() -> None:
    card = _card(registered())
    assert card.status is Status.REGISTERED
    assert card.registered_at == utc(2026, 11, 20)
    assert card.registered_plan is not None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"threshold_t": 2.4}, "below the floor"),
        ({"threshold_t": 2.8}, "needs threshold_reason"),
        ({"dsr_min": 0.90}, "looser"),
        ({"pbo_max": 0.20}, "looser"),
        ({"vault": {"start": "2028-02-01", "end": "2028-06-30"}}, "not overlap"),
        ({"confirmation": {"start": "2027-07-01", "end": "2027-06-01"}}, "before it starts"),
    ],
)
def test_plans_cannot_be_looser_than_the_protocol(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _plan(**changes)


def test_a_lower_threshold_with_a_reason_is_allowed() -> None:
    assert _plan(threshold_t=2.8, threshold_reason="clear mechanism, documented").threshold_t == 2.8


def _history(*entries: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"at": f"2026-11-{13 + i:02d}T00:00:00Z", **e} for i, e in enumerate(entries)]


FP = str(digest_bytes(b"plan"))


@pytest.mark.parametrize(
    ("history", "message"),
    [
        (_history({"status": "exploring"}), "start with status draft"),
        (
            _history({"status": "draft"}, {"status": "confirmed"}),
            "cannot go from draft to confirmed",
        ),
        (_history({"status": "draft"}, {"status": "registered"}), "plan_fingerprint"),
        (_history({"status": "draft", "plan_fingerprint": FP}), "plan_fingerprint"),
        (
            _history(
                {"status": "draft"},
                {"status": "registered", "plan_fingerprint": FP},
                {"status": "rejected"},
                {"status": "confirmed"},
            ),
            "cannot go from rejected",
        ),
        (
            list(reversed(_history({"status": "draft"}, {"status": "exploring"}))),
            "start with status draft",
        ),
    ],
)
def test_status_history_rules(history: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _card(history=history)


def test_history_must_be_chronological() -> None:
    history = [
        {"at": "2026-11-14T00:00:00Z", "status": "draft"},
        {"at": "2026-11-13T00:00:00Z", "status": "exploring"},
    ]
    with pytest.raises(ValidationError, match="chronological"):
        _card(history=history)


@pytest.mark.parametrize(
    "changes",
    [
        {"prior": 1.5},
        {"id": "H-1"},
        {"program": "P9"},
        {"return_source": "luck"},
        {"colour": "red"},
    ],
)
def test_malformed_cards(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _card(**changes)


def test_snapshot_sources_cannot_be_redownloadable() -> None:
    raw = tomllib.loads(SOURCE)
    assert Source.model_validate(raw).id == "deribit.book_summary"
    with pytest.raises(ValidationError, match="snapshot"):
        Source.model_validate({**raw, "redownloadable": True})


def _trial(**changes: object) -> TrialRecord:
    m = manifest()
    raw: dict[str, object] = {
        "trial_id": "T-000001",
        "recorded_at": "2026-11-13T04:00:00Z",
        "hypothesis": "H-0001",
        "segment": "exploration",
        "manifest": m.to_dict(),
        "result_fingerprint": str(m.fingerprint),
        "metrics": {"t": 1.2},
        "prev": None,
    }
    raw.update(changes)
    return TrialRecord.model_validate(raw)


def test_trial_records() -> None:
    assert _trial().segment is Segment.EXPLORATION
    with pytest.raises(ValidationError, match="local changes"):
        _trial(
            manifest=manifest(dirty=True).to_dict(),
            result_fingerprint=str(manifest(dirty=True).fingerprint),
        )
    with pytest.raises(ValidationError, match="does not match"):
        _trial(result_fingerprint=str(digest_bytes(b"something else")))
    with pytest.raises(ValidationError, match="vault"):
        _trial(segment="vault")
    with pytest.raises(ValidationError, match="vault"):
        _trial(vault_approval="D-0001")
    with pytest.raises(ValidationError):
        _trial(recorded_at="2026-11-13T04:00:00")  # no time zone
    with pytest.raises(ValidationError):
        _trial(metrics={"t": float("nan")} | {"x": "high"})
