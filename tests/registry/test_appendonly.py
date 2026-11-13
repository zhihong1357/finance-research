import json
import shutil
import subprocess
from pathlib import Path

import pytest

from finresearch.core.fingerprint import canonical_json
from finresearch.core.time import utc
from finresearch.registry.appendonly import check_append_only
from finresearch.registry.ledger import append_trial
from finresearch.registry.models import Segment
from finresearch.registry.validate import TRIALS, check
from tests.registry.conftest import (
    CONFIG,
    OUTPUT,
    manifest,
    registered,
    write_registry,
    write_report,
)

GIT = shutil.which("git") or "git"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(  # noqa: S603 - test helper with fixed arguments
        [GIT, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _append(repo: Path, t: float, at_day: int) -> None:
    trial = append_trial(
        repo / TRIALS,
        hypothesis="H-0001",
        segment=Segment.EXPLORATION,
        manifest=manifest(),
        config=CONFIG,
        output_fingerprint=OUTPUT,
        metrics={"t": t},
        recorded_at=utc(2026, 11, at_day),
    )
    write_report(repo, trial)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    r = write_registry(tmp_path / "repo", registered())
    _git(r, "init", "-q")
    _append(r, 1.0, 21)
    return r, _commit(r, "base")


def test_appending_is_allowed(repo: tuple[Path, str]) -> None:
    root, base = repo
    _append(root, 2.0, 22)
    _commit(root, "append")
    assert check_append_only(root, base) == []
    assert check(root) == []


def test_a_consistently_rewritten_ledger_is_still_caught(repo: tuple[Path, str]) -> None:
    root, base = repo
    row = json.loads((root / TRIALS).read_bytes().splitlines()[0])
    row["metrics"]["t"] = 4.2  # a better result, with a fresh, valid chain
    (root / TRIALS).write_bytes(canonical_json({**row, "prev": None}) + b"\n")
    assert check(root) == []  # the hash chain alone cannot see this
    problems = check_append_only(root, base)
    assert any("existing lines were changed or removed" in p for p in problems)


def test_deleting_append_only_files_and_cards(repo: tuple[Path, str]) -> None:
    root, base = repo
    (root / TRIALS).unlink()
    (root / "registry/hypotheses/H-0001.toml").unlink()
    problems = check_append_only(root, base)
    assert any("deleted, but it is append-only" in p for p in problems)
    assert any("hypothesis cards are never deleted" in p for p in problems)


def test_card_history_and_frozen_plan(repo: tuple[Path, str]) -> None:
    root, base = repo
    card = root / "registry/hypotheses/H-0001.toml"
    text = card.read_text()
    # change the plan and re-record a matching fingerprint: the file is consistent, history is not
    edited = registered(
        text.split("\n[[history]]")[0].replace('metric = "difference', 'metric = "ratio')
        + "\n"
        + '[[history]]\nat = 2026-11-13T04:00:00Z\nstatus = "draft"\n'
    )
    card.write_text(edited)
    assert check(root) == []
    problems = check_append_only(root, base)
    assert any("plan was changed after registration" in p for p in problems)
    assert any("earlier history entries were changed" in p for p in problems)


def test_first_push_and_unknown_base(repo: tuple[Path, str]) -> None:
    root, _ = repo
    assert check_append_only(root, "0" * 40) == []
    assert any("not found" in p for p in check_append_only(root, "f" * 40))
