import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from finresearch.core.time import NaiveDatetimeError, utc
from finresearch.data.pit import (
    Clock,
    LookaheadError,
    NotRegisteredError,
    PointInTime,
    VaultSealedError,
    VaultToken,
    clock_for,
    end_of_day,
    issue_vault_token,
)
from finresearch.data.table import PitTable, Snapshot
from finresearch.registry.ledger import append_owner_decision, append_trial
from finresearch.registry.models import DecisionKind, HypothesisCard, Segment
from finresearch.registry.validate import OWNER_DECISIONS, TRIALS, check
from tests.data.conftest import make_table
from tests.registry.conftest import (
    CARD,
    CONFIG,
    OUTPUT,
    manifest,
    registered,
    write_registry,
    write_report,
)

NOW = utc(2028, 9, 1)
TOKEN = VaultToken("D-0001", "H-0001")


def _card(text: str) -> HypothesisCard:
    return HypothesisCard.model_validate(tomllib.loads(text))


def test_end_of_day_is_inclusive() -> None:
    assert end_of_day(utc(2027, 6, 30).date()) == utc(2027, 6, 30, 23, 59, 59, 999_999)


def test_queries_stop_at_the_clock_limit(macro: PitTable) -> None:
    pit = PointInTime(Clock(limit=utc(2026, 11, 1)), [macro])
    assert pit.as_of("macro", utc(2026, 11, 1)).num_rows == 2
    assert pit.latest("macro").num_rows == 2  # the September CPI revision is not visible yet
    with pytest.raises(LookaheadError):
        pit.as_of("macro", utc(2026, 11, 1) + timedelta(microseconds=1))


def test_snapshots_are_plain_python(macro: PitTable) -> None:
    snap = PointInTime(Clock(limit=utc(2026, 11, 1)), [macro]).as_of("macro", utc(2026, 11, 1))
    assert (snap.name, snap.at) == ("macro", utc(2026, 11, 1))
    assert snap.column("value") == [3.0, 1.0]
    assert [r["series"] for r in snap.rows()] == ["CPI", "PPI"]


def tomorrows_value(pit: PointInTime, name: str, today: datetime) -> Snapshot:
    """What lookahead looks like in research code: peeking one day ahead."""
    return pit.as_of(name, today + timedelta(days=1))


def test_lookahead_code_fails(macro: PitTable) -> None:
    pit = PointInTime(Clock(limit=utc(2026, 11, 13)), [macro])
    with pytest.raises(LookaheadError):
        tomorrows_value(pit, "macro", utc(2026, 11, 13))


def test_a_view_is_the_door_at_decision_time(macro: PitTable) -> None:
    pit = PointInTime(Clock(limit=utc(2026, 12, 1)), [macro])
    decision = utc(2026, 11, 1)
    view = pit.view(decision)
    assert view.clock.limit == decision
    assert view.latest("macro").column("value") == [3.0, 1.0]  # first CPI release only
    with pytest.raises(LookaheadError):
        tomorrows_value(view, "macro", decision)  # the revision exists, but not yet
    assert pit.latest("macro").column("value") == [3.1, 2.9, 1.0]
    with pytest.raises(LookaheadError):
        pit.view(utc(2026, 12, 2))  # a view cannot reach past its parent
    with pytest.raises(LookaheadError):
        view.view(decision + timedelta(microseconds=1))


def test_views_share_the_provenance_record(macro: PitTable) -> None:
    other = make_table(name="other")
    pit = PointInTime(Clock(limit=utc(2026, 12, 1)), [macro, other])
    pit.view(utc(2026, 11, 1)).as_of("other", utc(2026, 11, 1))
    assert pit.used == {"other": other.fingerprint}


def test_times_must_carry_a_time_zone(macro: PitTable) -> None:
    pit = PointInTime(Clock(limit=utc(2026, 12, 1)), [macro])
    with pytest.raises(NaiveDatetimeError):
        pit.as_of("macro", datetime(2026, 11, 1))  # noqa: DTZ001 - the point of the test
    with pytest.raises(NaiveDatetimeError):
        Clock(limit=datetime(2026, 11, 1))  # noqa: DTZ001


def test_the_vault_is_sealed_without_a_token(macro: PitTable) -> None:
    vault = utc(2026, 11, 1)
    with pytest.raises(VaultSealedError):
        Clock(limit=utc(2026, 12, 1), vault_start=vault)
    sealed = PointInTime(Clock(limit=utc(2026, 10, 31, 23, 59), vault_start=vault), [macro])
    sealed.as_of("macro", utc(2026, 10, 31))
    assert not sealed.vault_crossed
    opened = PointInTime(Clock(limit=utc(2026, 12, 1), vault_start=vault, token=TOKEN), [macro])
    opened.as_of("macro", utc(2026, 10, 31))
    assert not opened.vault_crossed
    opened.view(utc(2026, 11, 2)).as_of("macro", utc(2026, 11, 2))
    assert opened.vault_crossed  # seen through a view, recorded on the door


def test_reads_are_recorded_for_provenance(macro: PitTable) -> None:
    other = make_table(name="other")
    pit = PointInTime(Clock(limit=utc(2026, 12, 1)), [macro, other])
    assert pit.names == ["macro", "other"]
    assert pit.used == {}
    pit.as_of("macro", utc(2026, 11, 1))
    pit.as_of("other", utc(2026, 11, 1))
    pit.as_of("macro", utc(2026, 11, 20))
    assert pit.used == {"macro": macro.fingerprint, "other": other.fingerprint}
    with pytest.raises(KeyError, match="no table 'missing'"):
        pit.as_of("missing", utc(2026, 11, 1))
    impostor = make_table(name="macro", rows=[("X", utc(2026, 1, 1), utc(2026, 1, 2), 0.0)])
    with pytest.raises(ValueError, match="two tables named 'macro'"):
        PointInTime(Clock(limit=utc(2026, 12, 1)), [macro, impostor])


def test_clock_for_each_segment() -> None:
    draft, reg = _card(CARD), _card(registered())
    exploring = clock_for(draft, Segment.EXPLORATION, NOW)
    assert exploring.limit == utc(2027, 6, 30, 23, 59, 59, 999_999)
    assert exploring.vault_start == utc(2028, 3, 1)
    assert clock_for(reg, Segment.CONFIRMATION, NOW).limit == utc(2028, 2, 29, 23, 59, 59, 999_999)
    assert clock_for(reg, Segment.VAULT, NOW, TOKEN).limit == utc(2028, 6, 30, 23, 59, 59, 999_999)
    assert clock_for(reg, Segment.FORWARD, NOW, TOKEN).limit == NOW
    assert clock_for(draft, Segment.SYNTHETIC, NOW) == Clock(limit=NOW)


def test_clock_for_refuses_what_the_protocol_forbids() -> None:
    draft, reg = _card(CARD), _card(registered())
    with pytest.raises(NotRegisteredError, match="registered"):
        clock_for(draft, Segment.CONFIRMATION, NOW)
    with pytest.raises(VaultSealedError):
        clock_for(reg, Segment.VAULT, NOW)
    with pytest.raises(VaultSealedError):
        clock_for(reg, Segment.FORWARD, NOW, VaultToken("D-0001", "H-0002"))  # someone else's token
    no_exploration = CARD.replace("[plan.exploration]\nstart = 2026-10-26\nend = 2027-06-30\n", "")
    with pytest.raises(ValueError, match="no exploration segment"):
        clock_for(_card(no_exploration), Segment.EXPLORATION, NOW)


def test_tokens_come_only_from_the_owners_decisions(tmp_path: Path) -> None:
    root = write_registry(tmp_path, registered())
    with pytest.raises(VaultSealedError, match="has not approved"):
        issue_vault_token(root, "H-0001")
    log = root / OWNER_DECISIONS
    append_owner_decision(
        log,
        kind=DecisionKind.GATE,
        subject="H-0001",
        summary="not a vault decision",
        recorded_at=NOW,
    )
    with pytest.raises(VaultSealedError):
        issue_vault_token(root, "H-0001")
    append_owner_decision(
        log, kind=DecisionKind.VAULT_ACCESS, subject="H-0001", summary="open it", recorded_at=NOW
    )
    assert issue_vault_token(root, "H-0001") == VaultToken("D-0002", "H-0001")


def test_clocks_agree_with_the_ledger_rules(tmp_path: Path) -> None:
    root = write_registry(tmp_path, registered())
    card = _card(registered())
    for segment, at in (
        (Segment.EXPLORATION, utc(2026, 11, 14)),
        (Segment.CONFIRMATION, utc(2028, 3, 1)),
    ):
        trial = append_trial(
            root / TRIALS,
            hypothesis="H-0001",
            segment=segment,
            manifest=manifest(clock_limit=clock_for(card, segment, NOW).limit),
            config=CONFIG,
            output_fingerprint=OUTPUT,
            metrics={"t": 1.0},
            recorded_at=at,
        )
        write_report(root, trial)
    assert check(root) == []
