import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from finresearch.core.time import NaiveDatetimeError, utc
from finresearch.data.pit import (
    Clock,
    LookaheadError,
    PointInTime,
    VaultSealedError,
    VaultToken,
    clock_for,
    end_of_day,
    issue_vault_token,
)
from finresearch.data.table import PitTable
from finresearch.registry.ledger import append_owner_decision, append_trial
from finresearch.registry.models import DecisionKind, HypothesisCard, Segment
from finresearch.registry.validate import OWNER_DECISIONS, TRIALS, check
from tests.data.conftest import make_table
from tests.registry.conftest import CARD, manifest, registered, write_registry

NOW = utc(2028, 9, 1)
TOKEN = VaultToken("D-0001", "H-0001")


def _card(text: str) -> HypothesisCard:
    return HypothesisCard.model_validate(tomllib.loads(text))


def test_end_of_day_is_inclusive() -> None:
    assert end_of_day(utc(2027, 6, 30).date()) == utc(2027, 6, 30, 23, 59, 59, 999_999)


def test_queries_stop_at_the_clock_limit(macro: PitTable) -> None:
    pit = PointInTime(Clock(limit=utc(2026, 11, 1)))
    assert pit.as_of(macro, utc(2026, 11, 1)).num_rows == 2
    assert pit.latest(macro).num_rows == 2  # the September CPI revision is not visible yet
    with pytest.raises(LookaheadError):
        pit.as_of(macro, utc(2026, 11, 1) + timedelta(microseconds=1))


def tomorrows_value(pit: PointInTime, table: PitTable, today: datetime) -> pa.Table:
    """What lookahead looks like in research code: peeking one day ahead."""
    return pit.as_of(table, today + timedelta(days=1))


def test_lookahead_code_fails(macro: PitTable) -> None:
    pit = PointInTime(Clock(limit=utc(2026, 11, 13)))
    with pytest.raises(LookaheadError):
        tomorrows_value(pit, macro, utc(2026, 11, 13))


def test_times_must_carry_a_time_zone(macro: PitTable) -> None:
    pit = PointInTime(Clock(limit=utc(2026, 12, 1)))
    with pytest.raises(NaiveDatetimeError):
        pit.as_of(macro, datetime(2026, 11, 1))  # noqa: DTZ001 - the point of the test
    with pytest.raises(NaiveDatetimeError):
        Clock(limit=datetime(2026, 11, 1))  # noqa: DTZ001


def test_the_vault_is_sealed_without_a_token(macro: PitTable) -> None:
    vault = utc(2026, 11, 1)
    with pytest.raises(VaultSealedError):
        Clock(limit=utc(2026, 12, 1), vault_start=vault)
    sealed = PointInTime(Clock(limit=utc(2026, 10, 31, 23, 59), vault_start=vault))
    sealed.as_of(macro, utc(2026, 10, 31))
    assert not sealed.vault_crossed
    opened = PointInTime(Clock(limit=utc(2026, 12, 1), vault_start=vault, token=TOKEN))
    opened.as_of(macro, utc(2026, 10, 31))
    assert not opened.vault_crossed
    opened.as_of(macro, utc(2026, 11, 2))
    assert opened.vault_crossed


def test_reads_are_recorded_for_provenance(macro: PitTable) -> None:
    pit = PointInTime(Clock(limit=utc(2026, 12, 1)))
    other = make_table(name="other")
    pit.as_of(macro, utc(2026, 11, 1))
    pit.as_of(other, utc(2026, 11, 1))
    pit.as_of(macro, utc(2026, 11, 20))
    assert pit.used == {"macro": macro.fingerprint, "other": other.fingerprint}
    impostor = make_table(name="macro", rows=[("X", utc(2026, 1, 1), utc(2026, 1, 2), 0.0)])
    with pytest.raises(ValueError, match="two different tables named 'macro'"):
        pit.as_of(impostor, utc(2026, 11, 1))


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
    with pytest.raises(ValueError, match="registered"):
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
        append_trial(
            root / TRIALS,
            hypothesis="H-0001",
            segment=segment,
            manifest=manifest(clock_limit=clock_for(card, segment, NOW).limit),
            metrics={"t": 1.0},
            recorded_at=at,
        )
    assert check(root) == []
