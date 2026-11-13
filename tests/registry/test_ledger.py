import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from finresearch.core.fingerprint import canonical_json, digest_bytes
from finresearch.core.time import utc
from finresearch.registry.ledger import (
    ChainError,
    append_owner_decision,
    append_refusal,
    append_trial,
    read_chain,
)
from finresearch.registry.models import DecisionKind, Refusal, RefusalKind, Segment
from tests.registry.conftest import CONFIG, OUTPUT, manifest


def _two_trials(path: Path) -> None:
    for i, t in enumerate((1.2, 2.5)):
        append_trial(
            path,
            hypothesis="H-0001",
            segment=Segment.EXPLORATION,
            manifest=manifest(),
            config=CONFIG,
            output_fingerprint=OUTPUT,
            metrics={"t": t},
            recorded_at=utc(2026, 11, 13, 4, i),
        )


def test_append_builds_a_hash_chain(tmp_path: Path) -> None:
    ledger = tmp_path / "trials.jsonl"
    _two_trials(ledger)
    lines = ledger.read_bytes().split(b"\n")[:-1]
    records = read_chain(ledger)
    assert [r["trial_id"] for r in records] == ["T-000001", "T-000002"]
    assert records[0]["prev"] is None
    assert records[1]["prev"] == str(digest_bytes(lines[0]))
    assert b'"recorded_at":"2026-11-13T04:00:00.000000Z"' in lines[0]  # canonical UTC text


def _rewrite(path: Path, lines: list[bytes]) -> None:
    path.write_bytes(b"".join(line + b"\n" for line in lines))


def test_editing_an_earlier_line_breaks_the_chain(tmp_path: Path) -> None:
    ledger = tmp_path / "trials.jsonl"
    _two_trials(ledger)
    lines = ledger.read_bytes().split(b"\n")[:-1]
    first = json.loads(lines[0])
    first["metrics"]["t"] = 9.9  # a better-looking result, still in canonical form
    _rewrite(ledger, [canonical_json(first), lines[1]])
    with pytest.raises(ChainError, match=r":2: hash chain broken"):
        read_chain(ledger)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda ls: [ls[1]], r":1: hash chain broken"),  # first line deleted
        (lambda ls: [ls[1], ls[0]], r":1: hash chain broken"),  # reordered
        (
            lambda ls: [ls[0].replace(b",", b", ", 1), ls[1]],
            "canonical form",
        ),  # hand-edited spacing
        (lambda ls: [b"not json", ls[1]], "not JSON"),
        (lambda ls: [b"[1,2]", ls[1]], "JSON object"),
    ],
)
def test_other_tampering(
    tmp_path: Path, mutate: Callable[[list[bytes]], list[bytes]], message: str
) -> None:
    ledger = tmp_path / "trials.jsonl"
    _two_trials(ledger)
    lines = ledger.read_bytes().split(b"\n")[:-1]
    _rewrite(ledger, mutate(lines))
    with pytest.raises(ChainError, match=message):
        read_chain(ledger)


def test_unterminated_last_line(tmp_path: Path) -> None:
    ledger = tmp_path / "trials.jsonl"
    _two_trials(ledger)
    ledger.write_bytes(ledger.read_bytes()[:-1])
    with pytest.raises(ChainError, match="not terminated"):
        read_chain(ledger)


def test_missing_and_empty_files_are_empty_chains(tmp_path: Path) -> None:
    assert read_chain(tmp_path / "none.jsonl") == []
    (tmp_path / "empty.jsonl").write_bytes(b"")
    assert read_chain(tmp_path / "empty.jsonl") == []


def test_a_dirty_run_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    ledger = tmp_path / "trials.jsonl"
    _two_trials(ledger)
    before = ledger.read_bytes()
    with pytest.raises(ValidationError, match="local changes"):
        append_trial(
            ledger,
            hypothesis="H-0001",
            segment=Segment.EXPLORATION,
            manifest=manifest(dirty=True),
            config=CONFIG,
            output_fingerprint=OUTPUT,
            metrics={"t": 3.1},
            recorded_at=utc(2026, 11, 13, 5),
        )
    assert ledger.read_bytes() == before


def test_owner_decisions(tmp_path: Path) -> None:
    log = tmp_path / "owner_decisions.jsonl"
    d1 = append_owner_decision(
        log,
        kind=DecisionKind.VAULT_ACCESS,
        subject="H-0001",
        summary="open the vault",
        recorded_at=utc(2028, 7, 1),
    )
    d2 = append_owner_decision(
        log, kind=DecisionKind.GATE, summary="paper trading", recorded_at=utc(2028, 8, 1)
    )
    assert (d1.decision_id, d2.decision_id) == ("D-0001", "D-0002")
    assert [r["decision_id"] for r in read_chain(log)] == ["D-0001", "D-0002"]


def _refuse(path: Path, at: int, kind: RefusalKind = RefusalKind.LOOKAHEAD) -> Refusal:
    return append_refusal(
        path,
        hypothesis="H-0001",
        segment=Segment.EXPLORATION,
        study="thin_slice",
        kind=kind,
        detail="asked about 2018-06-01, but this door may only see up to 2011-12-31",
        commit="a" * 40,
        dirty=True,
        recorded_at=utc(2026, 11, 13, at),
    )


def test_refusals_are_chained_like_the_ledger(tmp_path: Path) -> None:
    log = tmp_path / "refusals.jsonl"
    first = _refuse(log, 1)
    second = _refuse(log, 2, RefusalKind.VAULT_SEALED)
    assert (first.refusal_id, second.refusal_id) == ("Q-000001", "Q-000002")
    assert second.prev == str(digest_bytes(log.read_bytes().split(b"\n")[0]))
    assert [r["kind"] for r in read_chain(log)] == ["lookahead", "vault_sealed"]
    with pytest.raises(ValidationError, match="commit"):
        append_refusal(
            log,
            hypothesis="H-0001",
            segment=Segment.EXPLORATION,
            study="thin_slice",
            kind=RefusalKind.LOOKAHEAD,
            detail="x",
            commit="not-a-commit",
            dirty=False,
            recorded_at=utc(2026, 11, 13, 3),
        )
    assert len(read_chain(log)) == 2
