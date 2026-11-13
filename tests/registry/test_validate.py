from datetime import datetime
from pathlib import Path

from finresearch.core.fingerprint import canonical_json, digest_bytes
from finresearch.core.time import utc
from finresearch.registry.ledger import (
    append_owner_decision,
    append_refusal,
    append_trial,
    read_chain,
)
from finresearch.registry.models import DecisionKind, RefusalKind, Segment
from finresearch.registry.validate import OWNER_DECISIONS, REFUSALS, TRIALS, check
from tests.registry.conftest import (
    CARD,
    CONFIG,
    END_OF_EXPLORATION,
    OUTPUT,
    manifest,
    registered,
    write_registry,
    write_report,
)

REPO = Path(__file__).resolve().parents[2]
END_OF_CONFIRMATION = utc(2028, 2, 29)


def _trial(
    root: Path,
    segment: Segment,
    at: datetime,
    clock: datetime | None = END_OF_EXPLORATION,
    hypothesis: str = "H-0001",
    vault_approval: str | None = None,
) -> None:
    trial = append_trial(
        root / TRIALS,
        hypothesis=hypothesis,
        segment=segment,
        manifest=manifest(clock_limit=clock),
        config=CONFIG,
        output_fingerprint=OUTPUT,
        metrics={"t": 1.0},
        recorded_at=at,
        vault_approval=vault_approval,
    )
    write_report(root, trial)


def _has(problems: list[str], fragment: str) -> bool:
    return any(fragment in p for p in problems)


def test_this_repository_is_consistent() -> None:
    assert check(REPO) == []


def test_a_small_valid_registry(root: Path) -> None:
    _trial(root, Segment.EXPLORATION, utc(2026, 11, 14))
    assert check(root) == []


def test_editing_the_plan_after_registration_is_detected(tmp_path: Path) -> None:
    card = registered().replace('metric = "difference', 'metric = "ratio')
    assert _has(check(write_registry(tmp_path, card)), "plan was changed after registration")
    assert check(write_registry(tmp_path / "ok", registered())) == []


def test_file_and_reference_problems(tmp_path: Path) -> None:
    problems = check(write_registry(tmp_path, source=None))
    assert _has(problems, "unknown data source deribit.book_summary")
    bad = write_registry(tmp_path / "bad", card=None)
    (bad / "registry/hypotheses/H-0002.toml").write_text(CARD, encoding="utf-8")
    (bad / "registry/hypotheses/H-0003.toml").write_text("id = [", encoding="utf-8")
    problems = check(bad)
    assert _has(problems, "file name must be the id")
    assert _has(problems, "not valid TOML")


def test_confirmation_needs_registration_first(tmp_path: Path) -> None:
    root = write_registry(tmp_path, registered())
    _trial(
        root, Segment.CONFIRMATION, utc(2026, 11, 19), clock=END_OF_CONFIRMATION
    )  # a day too early
    assert _has(check(root), "confirmation trial before H-0001 was registered")


def test_one_confirmation_only(tmp_path: Path) -> None:
    root = write_registry(tmp_path, registered())
    _trial(root, Segment.CONFIRMATION, utc(2028, 3, 1), clock=END_OF_CONFIRMATION)
    assert check(root) == []
    _trial(root, Segment.CONFIRMATION, utc(2028, 3, 2), clock=END_OF_CONFIRMATION)
    assert _has(check(root), "2 confirmation trials")


def test_clock_limit_stays_inside_the_segment(root: Path) -> None:
    _trial(
        root, Segment.EXPLORATION, utc(2026, 11, 14), clock=utc(2027, 7, 1)
    )  # peeks into confirmation
    _trial(root, Segment.EXPLORATION, utc(2026, 11, 15), clock=None)
    problems = check(root)
    assert sum("clock limit passes the end of the exploration segment" in p for p in problems) == 2


def test_confirmed_status_needs_a_confirmation_trial(tmp_path: Path) -> None:
    card = registered() + '\n[[history]]\nat = 2028-03-05T00:00:00Z\nstatus = "confirmed"\n'
    root = write_registry(tmp_path, card)
    assert _has(check(root), "status confirmed needs a confirmation trial")
    _trial(root, Segment.CONFIRMATION, utc(2028, 3, 1), clock=END_OF_CONFIRMATION)
    assert check(root) == []


def test_vault_access_needs_the_owners_decision(tmp_path: Path) -> None:
    root = write_registry(tmp_path, registered())
    end_of_vault = utc(2028, 6, 30)
    _trial(root, Segment.VAULT, utc(2028, 7, 2), clock=end_of_vault, vault_approval="D-0001")
    assert _has(check(root), "without the owner's vault_access decision")

    append_owner_decision(
        root / OWNER_DECISIONS,
        kind=DecisionKind.VAULT_ACCESS,
        subject="H-0001",
        summary="ok",
        recorded_at=utc(2028, 7, 1),
    )
    assert check(root) == []


def test_vault_approval_must_come_first_and_name_the_hypothesis(tmp_path: Path) -> None:
    root = write_registry(tmp_path, registered())
    log = root / OWNER_DECISIONS
    append_owner_decision(
        log,
        kind=DecisionKind.VAULT_ACCESS,
        subject="H-0001",
        summary="ok",
        recorded_at=utc(2028, 7, 3),
    )
    append_owner_decision(
        log,
        kind=DecisionKind.VAULT_ACCESS,
        subject="H-0009",
        summary="?",
        recorded_at=utc(2028, 7, 3),
    )
    _trial(root, Segment.VAULT, utc(2028, 7, 2), clock=utc(2028, 6, 30), vault_approval="D-0001")
    _trial(root, Segment.VAULT, utc(2028, 7, 4), clock=utc(2028, 6, 30), vault_approval="D-0002")
    problems = check(root)
    assert _has(problems, "vault trial recorded before the owner's approval")
    assert _has(problems, "D-0002 grants vault access to unknown 'H-0009'")
    assert _has(problems, f"{TRIALS}:2: vault trial without the owner's vault_access decision")


def test_trials_for_unknown_hypotheses(root: Path) -> None:
    _trial(root, Segment.EXPLORATION, utc(2026, 11, 14), hypothesis="H-0042")
    assert _has(check(root), "unknown hypothesis H-0042")


def test_ids_must_run_in_sequence_and_time_forward(root: Path) -> None:
    _trial(root, Segment.EXPLORATION, utc(2026, 11, 14))
    _trial(root, Segment.EXPLORATION, utc(2026, 11, 13))  # recorded earlier than the line before
    rows = read_chain(root / TRIALS)
    rows[1]["trial_id"] = "T-000005"
    prev = None
    lines: list[bytes] = []
    for row in rows:  # rebuild a valid chain around the edited id
        line = canonical_json({**row, "prev": prev})
        lines.append(line)
        prev = str(digest_bytes(line))
    (root / TRIALS).write_bytes(b"".join(line + b"\n" for line in lines))
    problems = check(root)
    assert _has(problems, "id T-000005 out of sequence")
    assert _has(problems, "recorded_at goes backwards")


def test_refusals_are_checked(root: Path) -> None:
    log = root / REFUSALS
    for hypothesis in ("H-0001", "H-0002"):
        append_refusal(
            log,
            hypothesis=hypothesis,
            segment=Segment.VAULT,
            study="s",
            kind=RefusalKind.VAULT_SEALED,
            detail="the owner has not approved opening the vault",
            commit="b" * 40,
            dirty=False,
            recorded_at=utc(2026, 11, 13),
        )
    assert check(root) == [f"{REFUSALS}:2: unknown hypothesis H-0002"]
    lines = log.read_bytes().split(b"\n")
    log.write_bytes(lines[1] + b"\n")
    assert any("hash chain broken" in p for p in check(root))
