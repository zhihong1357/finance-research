from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from finresearch.core.time import utc
from finresearch.registry.ledger import ChainError, append_boundaries
from finresearch.registry.models import Boundaries, DateRange, Universe
from finresearch.registry.validate import BOUNDARIES, check

EXPLORE = DateRange(start=date(2019, 12, 1), end=date(2024, 1, 31))
CONFIRM = DateRange(start=date(2024, 2, 1), end=date(2025, 10, 31))
VAULT = DateRange(start=date(2025, 11, 1), end=date(2026, 10, 31))
AT = utc(2026, 11, 16)


def test_the_repository_records_u1_once() -> None:
    root = Path(__file__).resolve().parents[2]
    assert check(root) == []
    lines = (root / BOUNDARIES).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    b = Boundaries.model_validate_json(lines[0])
    assert (b.universe, b.exploration, b.confirmation, b.vault) == (
        Universe.U1,
        EXPLORE,
        CONFIRM,
        VAULT,
    )


def test_a_second_record_for_the_same_universe_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "boundaries.jsonl"
    append_boundaries(
        path,
        universe=Universe.U1,
        exploration=EXPLORE,
        confirmation=CONFIRM,
        vault=VAULT,
        decision="0015",
        recorded_at=AT,
    )
    with pytest.raises(ChainError, match="never move"):
        append_boundaries(
            path,
            universe=Universe.U1,
            exploration=EXPLORE,
            confirmation=CONFIRM,
            vault=VAULT,
            decision="0015",
            recorded_at=AT,
        )


def test_segments_must_follow_one_another_with_no_gap() -> None:
    later = DateRange(start=date(2024, 2, 2), end=date(2025, 10, 31))  # one day left out
    with pytest.raises(ValidationError, match="no gap"):
        Boundaries(
            boundary_id="B-0001",
            recorded_at=AT,
            universe=Universe.U1,
            exploration=EXPLORE,
            confirmation=later,
            vault=VAULT,
            decision="0015",
            prev=None,
        )


def _repo(tmp_path: Path, card: str, records: list[str]) -> Path:
    (tmp_path / "registry" / "hypotheses").mkdir(parents=True)
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "docs" / "decisions" / "0015-u1-segments.md").write_text("x", encoding="utf-8")
    if card:
        (tmp_path / "registry" / "hypotheses" / "H-0001.toml").write_text(card, encoding="utf-8")
    for universe in records:
        append_boundaries(
            tmp_path / BOUNDARIES,
            universe=Universe(universe),
            exploration=EXPLORE,
            confirmation=CONFIRM,
            vault=VAULT,
            decision="0015",
            recorded_at=AT,
        )
    return tmp_path


def test_a_plan_outside_its_segment_fails_the_check(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "registry" / "hypotheses" / "H-0001.toml"
    card = source.read_text(encoding="utf-8")
    universe = next(line for line in card.splitlines() if line.startswith("universe"))
    card = card.replace(universe, 'universe = "U1"')
    problems = check(_repo(tmp_path, card, ["U1"]))
    assert any("outside U1's" in p for p in problems)


def test_a_missing_decision_record_fails_the_check(tmp_path: Path) -> None:
    root = _repo(tmp_path, "", ["U1"])
    (root / "docs" / "decisions" / "0015-u1-segments.md").unlink()
    assert any("decision 0015 not found" in p for p in check(root))
