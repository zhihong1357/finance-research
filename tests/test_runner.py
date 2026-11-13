"""The thin slice end to end, and the Phase 1.6 acceptance checks (docs/roadmap.md).

Each test builds a small git repository holding the real H-0001 card, its
source entry and the real lock file, then drives the run framework in it.
"""

import json
import shutil
import subprocess
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from finresearch.cli import main
from finresearch.core.time import utc
from finresearch.data.pit import (
    LookaheadError,
    NotRegisteredError,
    PointInTime,
    VaultSealedError,
)
from finresearch.registry.appendonly import check_append_only, check_commits
from finresearch.registry.ledger import read_chain
from finresearch.registry.models import RefusalKind, Segment, TrialRecord
from finresearch.registry.validate import REFUSALS, REPORTS, TRIALS, check
from finresearch.report.standard import parse
from finresearch.runner import RunError, RunOutput, execute, record, reproduce
from finresearch.studies import STUDIES
from finresearch.studies.base import Study, StudyResult
from finresearch.studies.thin_slice import INDICATOR, SOURCE
from finresearch.studies.thin_slice import compute as thin_slice
from tests.registry.conftest import registered

REPO = Path(__file__).resolve().parents[1]
CARD = REPO / "registry/hypotheses/H-0001.toml"
GIT = shutil.which("git") or "git"
NOW = utc(2026, 11, 13, 12)


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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in ("registry/hypotheses/H-0001.toml", "registry/sources/synthetic.thin_slice.toml"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    shutil.copy(REPO / "uv.lock", root / "uv.lock")
    _git(root, "init", "-q")
    _commit(root, "thin slice")
    return root


def _run(root: Path, tmp: Path, segment: Segment = Segment.EXPLORATION) -> RunOutput:
    work = tmp / f"work-{len(list(tmp.glob('work-*')))}"
    return execute(root, "thin_slice", "H-0001", segment, work=work, now=NOW, recorded_at=NOW)


def _refusals(root: Path) -> list[dict[str, Any]]:
    return read_chain(root / REFUSALS)


def _swap_study(monkeypatch: pytest.MonkeyPatch, compute: Any) -> None:  # noqa: ANN401
    monkeypatch.setitem(STUDIES, "thin_slice", Study("thin_slice", (SOURCE,), compute))


# ---------------------------------------------------------------- the slice itself


def test_a_run_from_a_blank_state_is_deterministic(repo: Path, tmp_path: Path) -> None:
    a, b = _run(repo, tmp_path), _run(repo, tmp_path)
    assert a.summary() == b.summary()
    assert a.manifest.reproducible
    assert a.manifest.clock_limit == utc(2011, 12, 31, 23, 59, 59, 999_999)
    assert sorted(a.manifest.data) == ["synthetic.indicator", "synthetic.returns"]


def test_the_door_keeps_the_revision_out(repo: Path, tmp_path: Path) -> None:
    m = _run(repo, tmp_path).metrics
    assert m["pairs"] == 141  # decisions 2000-03 .. 2011-11, all returns public by 2011-12-31
    assert m["pairs_differing"] == m["pairs"]  # every decision saw the first release
    assert m["corr_pit_low"] <= m["theory_pit"] <= m["corr_pit_high"]
    assert m["theory_pit"] == pytest.approx(0.5 / (1.25 * 1.64) ** 0.5)
    assert m["theory_revised"] == pytest.approx(0.5 / 1.25**0.5)
    assert m["corr_revised"] > m["corr_pit"]  # the naive backtest flatters the signal


def test_record_writes_the_ledger_and_the_report(repo: Path, tmp_path: Path) -> None:
    out = _run(repo, tmp_path)
    trial, path = record(repo, out, NOW)
    assert (trial.trial_id, path) == ("T-000001", repo / REPORTS / "T-000001.md")
    header, body = parse(path.read_text(encoding="utf-8"))
    assert (header.trial_id, header.output_fingerprint) == ("T-000001", trial.output_fingerprint)
    assert body == out.body
    assert trial.config == out.config
    assert check(repo) == []


def test_uncommitted_changes_cannot_be_recorded(repo: Path, tmp_path: Path) -> None:
    (repo / "notes.txt").write_text("work in progress", encoding="utf-8")
    out = _run(repo, tmp_path)
    assert not out.manifest.reproducible
    with pytest.raises(RunError, match="uncommitted"):
        record(repo, out, NOW)
    assert not (repo / TRIALS).exists()


def test_the_study_may_only_read_declared_sources(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        STUDIES, "thin_slice", Study("thin_slice", (SOURCE, "other.source"), thin_slice)
    )
    with pytest.raises(RunError, match="does not declare"):
        _run(repo, tmp_path)


# ---------------------------------------------------------------- acceptance: lookahead


def peek_after_the_decision(pit: PointInTime, config: Mapping[str, Any]) -> StudyResult:
    """Lookahead written into research code: at each decision, read next month's data."""
    for decision in sorted(r["event_time"] for r in pit.latest("synthetic.returns").rows()):
        pit.view(decision).as_of(INDICATOR, decision + timedelta(days=31))
    return thin_slice(pit, config)


def test_lookahead_code_fails_and_is_recorded(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _swap_study(monkeypatch, peek_after_the_decision)
    with pytest.raises(LookaheadError):
        _run(repo, tmp_path)
    (refusal,) = _refusals(repo)
    assert (refusal["kind"], refusal["study"]) == (RefusalKind.LOOKAHEAD, "thin_slice")
    assert refusal["commit"] == _git(repo, "rev-parse", "HEAD")
    assert check(repo) == []
    assert not (repo / TRIALS).exists()  # nothing was disclosed, nothing was recorded as a trial


# ---------------------------------------------------------------- acceptance: the vault


def query_the_vault(pit: PointInTime, config: Mapping[str, Any]) -> StudyResult:
    pit.as_of(INDICATOR, utc(2018, 6, 1))  # inside the vault (2017-01-01 .. 2019-12-31)
    return thin_slice(pit, config)


def test_a_query_past_the_vault_boundary_fails_and_is_recorded(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _swap_study(monkeypatch, query_the_vault)
    with pytest.raises(LookaheadError, match="2018-06-01"):
        _run(repo, tmp_path)
    assert [r["kind"] for r in _refusals(repo)] == [RefusalKind.LOOKAHEAD]


def test_the_vault_segment_needs_the_owners_approval(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(NotRegisteredError):
        _run(repo, tmp_path, Segment.CONFIRMATION)
    (repo / "registry/hypotheses/H-0001.toml").write_text(
        registered(CARD.read_text(encoding="utf-8")), encoding="utf-8"
    )
    with pytest.raises(VaultSealedError, match="has not approved"):
        _run(repo, tmp_path, Segment.VAULT)
    kinds = [r["kind"] for r in _refusals(repo)]
    assert kinds == [RefusalKind.NOT_REGISTERED, RefusalKind.VAULT_SEALED]
    assert check(repo) == []


def test_the_command_line_reports_a_refusal(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _swap_study(monkeypatch, query_the_vault)
    args = ["run", "--study", "thin_slice", "--hypothesis", "H-0001", "--segment", "exploration"]
    assert main([*args, "--root", str(repo)]) == 2
    printed = capsys.readouterr().out
    assert "refused:" in printed
    assert "recorded as Q-000001" in printed


# ---------------------------------------------------------------- acceptance: the ledger


@pytest.fixture
def recorded(repo: Path, tmp_path: Path) -> tuple[Path, str, TrialRecord]:
    trial, _ = record(repo, _run(repo, tmp_path), NOW)
    return repo, _commit(repo, "record T-000001"), trial


def test_deleting_a_ledger_entry_fails_the_checks(recorded: tuple[Path, str, TrialRecord]) -> None:
    root, base, _ = recorded
    (root / TRIALS).write_bytes(b"")
    assert "reports/T-000001.md: no trial T-000001 in the ledger" in check(root)
    assert any("changed or removed" in p for p in check_append_only(root, base))


def test_editing_a_report_fails_the_checks(recorded: tuple[Path, str, TrialRecord]) -> None:
    root, _, _ = recorded
    path = root / REPORTS / "T-000001.md"
    path.write_text(path.read_text(encoding="utf-8").replace("0.363", "0.400"), encoding="utf-8")
    assert check(root) == [
        f"{REPORTS}/T-000001.md: body does not match the output fingerprint (was it edited?)"
    ]
    path.unlink()
    assert check(root) == [f"{REPORTS}/T-000001.md: missing (every trial has a standard report)"]


# ---------------------------------------------------------------- acceptance: reproduction


def _in_process(_worktree: Path, args: list[str]) -> None:
    assert main(args) == 0


def test_a_recorded_trial_reproduces(recorded: tuple[Path, str, TrialRecord]) -> None:
    root, _, trial = recorded
    assert reproduce(root, trial, _in_process) == []
    assert _git(root, "worktree", "list").count("\n") == 0  # the worktree is cleaned up


def test_a_different_result_is_reported(recorded: tuple[Path, str, TrialRecord]) -> None:
    root, _, trial = recorded
    forged = trial.model_copy(update={"output_fingerprint": "sha256:" + "0" * 64})
    assert reproduce(root, forged, _in_process) == ["T-000001: output_fingerprint differs on rerun"]


def test_recorded_commits_must_stay_in_history(repo: Path, tmp_path: Path) -> None:
    main_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "notes.txt").write_text("a commit that will be squashed away", encoding="utf-8")
    _commit(repo, "side work")
    record(repo, _run(repo, tmp_path), NOW)
    ledger = (repo / TRIALS).read_bytes()
    report = (repo / REPORTS / "T-000001.md").read_bytes()
    _git(repo, "stash", "-q", "--include-untracked")
    _git(repo, "checkout", "-q", main_branch)
    (repo / TRIALS).write_bytes(ledger)  # what a squash merge leaves behind
    (repo / REPORTS).mkdir()
    (repo / REPORTS / "T-000001.md").write_bytes(report)
    _commit(repo, "squashed")
    (problem,) = check_commits(repo)
    assert "is not in this history" in problem
    assert check_commits(tmp_path / "missing") == []


def test_the_run_command_writes_a_summary(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "run.json"
    args = ["run", "--hypothesis", "H-0001", "--segment", "exploration", "--root", str(repo)]
    assert main([*args, "--study", "thin_slice", "--json-out", str(out)]) == 0
    summary = json.loads(out.read_bytes())
    assert summary["result_fingerprint"] == str(_run(repo, tmp_path).manifest.fingerprint)
    assert main([*args, "--study", "nope"]) == 1
