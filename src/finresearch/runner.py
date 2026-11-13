"""The run framework: from a hypothesis card to a ledger entry and a standard report.

A run, in order (docs/architecture.md section 3.1):

1. Load the registry and the card; the study may only read sources the card
   declares.
2. Note the code version *before* writing anything, so a clean checkout is
   recorded as clean.
3. Build the clock for the segment (``data.pit.clock_for``). The vault needs
   the owner's token; held-out segments need a registered card.
4. Rebuild the data from a blank directory: fetch -> raw layer -> normalized
   layer -> tables -> door.
5. Run the study behind the door and write the standard report body.
6. Optionally record: append the trial to the ledger and write the report.

A refused request (``AccessRefusedError``) is appended to
``registry/refusals.jsonl`` before the error propagates.

``reproduce`` checks out a recorded commit in a fresh worktree, installs its
locked environment, reruns it from a blank directory and compares both
fingerprints with the ledger.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from finresearch.core.config import load_toml
from finresearch.core.fingerprint import Fingerprint, digest_value
from finresearch.core.provenance import (
    CodeVersion,
    ProvenanceError,
    RunManifest,
    code_version,
    environment_fingerprint,
)
from finresearch.core.time import format_utc
from finresearch.data.catalog import load_tables, normalize
from finresearch.data.pit import (
    AccessRefusedError,
    LookaheadError,
    NotRegisteredError,
    PointInTime,
    VaultSealedError,
    clock_for,
    issue_vault_token,
)
from finresearch.data.raw import RawStore
from finresearch.data.table import PitTable
from finresearch.registry.ledger import append_refusal, append_trial
from finresearch.registry.models import (
    HypothesisCard,
    RefusalKind,
    Segment,
    TrialRecord,
)
from finresearch.registry.validate import REFUSALS, REPORTS, TRIALS, load
from finresearch.report.standard import Header, body_fingerprint, render, render_body
from finresearch.sources import ADAPTERS
from finresearch.studies import STUDIES
from finresearch.studies.base import STUDY_SECTIONS, Study

LOCK_FILE = "uv.lock"
_KINDS: dict[type[AccessRefusedError], RefusalKind] = {
    LookaheadError: RefusalKind.LOOKAHEAD,
    VaultSealedError: RefusalKind.VAULT_SEALED,
    NotRegisteredError: RefusalKind.NOT_REGISTERED,
}
_NEEDS_TOKEN = frozenset({Segment.VAULT, Segment.FORWARD})


class RunError(RuntimeError):
    """The run cannot start, or cannot be recorded, as asked."""


@dataclass(frozen=True)
class RunOutput:
    study: str
    hypothesis: str
    segment: Segment
    config: dict[str, Any]
    manifest: RunManifest
    metrics: dict[str, float]
    body: str

    @property
    def output_fingerprint(self) -> Fingerprint:
        return body_fingerprint(self.body)

    def summary(self) -> dict[str, Any]:
        """What ``--json-out`` writes, and what ``reproduce`` compares."""
        return {
            "study": self.study,
            "hypothesis": self.hypothesis,
            "segment": str(self.segment),
            "config": self.config,
            "manifest": self.manifest.to_dict(),
            "result_fingerprint": str(self.manifest.fingerprint),
            "output_fingerprint": str(self.output_fingerprint),
            "metrics": self.metrics,
        }


def study_config(name: str) -> dict[str, Any]:
    """The study's parameters, from ``config/studies/<name>.toml``; ``study`` names it."""
    return {"study": name, **load_toml(f"studies/{name}")}


def build_tables(study: Study, config: dict[str, Any], work: Path) -> list[PitTable]:
    """Fetch -> raw layer -> normalized layer -> tables, inside ``work``."""
    store = RawStore(work)
    tables: list[PitTable] = []
    for source in study.sources:
        adapter = ADAPTERS[source]
        for r in adapter.fetch(config):
            store.save(source, r.name, r.url, r.fetched_at, r.content)
        directory = work / "tables" / source
        normalize(store, source, adapter.parse, directory)
        tables.extend(load_tables(directory).values())
    return tables


def _resolve(root: Path, study_name: str, hypothesis: str) -> tuple[Study, HypothesisCard]:
    reg, problems = load(root)
    if problems:
        raise RunError(f"the registry has {len(problems)} problem(s): run `registry check`")
    study = STUDIES.get(study_name)
    if study is None:
        raise RunError(f"unknown study {study_name!r} (known: {sorted(STUDIES)})")
    entry = reg.hypotheses.get(hypothesis)
    if entry is None:
        raise RunError(f"unknown hypothesis {hypothesis}")
    undeclared = [s for s in study.sources if s not in entry.card.data]
    if undeclared:
        raise RunError(f"{study_name} reads {undeclared}, which {hypothesis} does not declare")
    return study, entry.card


def execute(
    root: Path,
    study_name: str,
    hypothesis: str,
    segment: Segment,
    *,
    work: Path,
    now: datetime,
    recorded_at: datetime,
) -> RunOutput:
    """Run ``study_name`` for ``hypothesis`` on ``segment``. Nothing is written to the ledger.

    ``now`` is the present as far as clocks are concerned (it bounds synthetic and
    forward runs); ``recorded_at`` stamps a refusal, if there is one.
    """
    study, card = _resolve(root, study_name, hypothesis)
    config = study_config(study_name)
    code = code_version(root)
    try:
        token = issue_vault_token(root, hypothesis) if segment in _NEEDS_TOKEN else None
        clock = clock_for(card, segment, now, token)
        pit = PointInTime(clock, build_tables(study, config, work))
        result = study.compute(pit, config)
    except AccessRefusedError as exc:
        _refuse(root, exc, code, hypothesis, segment, study_name, recorded_at)
        raise
    if frozenset(result.sections) != STUDY_SECTIONS:
        raise RunError(f"{study_name} must write report sections {sorted(STUDY_SECTIONS)}")
    manifest = RunManifest(
        code=code,
        environment=environment_fingerprint(root / LOCK_FILE),
        config=digest_value(config),
        data=pit.used,
        seed=int(config.get("seed", 0)),
        clock_limit=clock.limit,
    )
    sections = dict(result.sections)
    sections[2] = _hypothesis_section(card)
    sections[3] = f"{sections[3].strip()}\n\n{_data_table(manifest)}"
    sections[12] = _manifest_section(manifest, config)
    title = f"{card.id} · {card.title}"
    body = render_body(title, sections)
    return RunOutput(study_name, hypothesis, segment, config, manifest, result.metrics, body)


def _refuse(
    root: Path,
    exc: AccessRefusedError,
    code: CodeVersion,
    hypothesis: str,
    segment: Segment,
    study: str,
    recorded_at: datetime,
) -> None:
    append_refusal(
        root / REFUSALS,
        hypothesis=hypothesis,
        segment=segment,
        study=study,
        kind=_KINDS[type(exc)],
        detail=str(exc),
        commit=code.commit,
        dirty=code.dirty,
        recorded_at=recorded_at,
    )


def _hypothesis_section(card: HypothesisCard) -> str:
    plan = card.plan
    segments = [
        f"探索 {plan.exploration.start} 至 {plan.exploration.end}"
        if plan.exploration
        else "无探索段",
        f"确认 {plan.confirmation.start} 至 {plan.confirmation.end}",
        f"金库 {plan.vault.start} 至 {plan.vault.end}",
    ]
    return "\n".join(
        [
            f"假说卡：`registry/hypotheses/{card.id}.toml`（研究计划 {card.program}，"
            f"研究宇宙 {card.scope.universe}）。",
            "",
            f"- 机制：{card.mechanism}",
            f"- 证伪条件：{card.falsification}",
            f"- 三段划分：{'；'.join(segments)}。",
        ]
    )


def _data_table(manifest: RunManifest) -> str:
    rows = [f"| `{name}` | `{fp}` |" for name, fp in manifest.data.items()]
    return "\n".join(["本次运行读过的表：", "", "| 表 | 内容指纹 |", "|---|---|", *rows])


def _manifest_section(manifest: RunManifest, config: dict[str, Any]) -> str:
    def block(value: object) -> str:
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)

    return "\n".join(
        [
            f"结果指纹：`{manifest.fingerprint}`",
            "",
            "运行清单：",
            "",
            "```json",
            block(manifest.to_dict()),
            "```",
            "",
            "配置：",
            "",
            "```json",
            block(config),
            "```",
            "",
            "重现：`uv run finresearch reproduce --trial <本报告的试验编号>`。"
            "程序切换到运行清单里的代码版本，按锁定文件安装环境，从空白目录重建数据并重跑，"
            "核对结果指纹和输出指纹。",
        ]
    )


def record(root: Path, out: RunOutput, recorded_at: datetime) -> tuple[TrialRecord, Path]:
    """Append ``out`` to the ledger and write its standard report."""
    if not out.manifest.reproducible:
        raise RunError("the working tree has uncommitted changes: commit, then run again")
    trial = append_trial(
        root / TRIALS,
        hypothesis=out.hypothesis,
        segment=out.segment,
        manifest=out.manifest,
        config=out.config,
        metrics=out.metrics,
        output_fingerprint=out.output_fingerprint,
        recorded_at=recorded_at,
    )
    path = root / REPORTS / f"{trial.trial_id}.md"
    header = Header(
        trial_id=trial.trial_id,
        recorded_at=format_utc(trial.recorded_at),
        hypothesis=trial.hypothesis,
        segment=str(trial.segment),
        result_fingerprint=trial.result_fingerprint,
        output_fingerprint=trial.output_fingerprint,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:  # never overwrite a report
        fh.write(render(header, out.body))
    return trial, path


# ---------------------------------------------------------------- reproduction

Invoke = Callable[[Path, list[str]], None]


def _uv_invoke(worktree: Path, args: list[str]) -> None:
    """Run the checked-out commit's own command line in its own locked environment."""
    uv = shutil.which("uv")
    if uv is None:
        raise RunError("uv is not installed")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    subprocess.run(  # noqa: S603 - fixed executable, arguments built here
        [uv, "run", "--locked", "--quiet", "--directory", str(worktree), "finresearch", *args],
        check=True,
        env=env,
    )


def _git(repo: Path, *args: str) -> None:
    git = shutil.which("git")
    if git is None:
        raise ProvenanceError("git is not installed")
    result = subprocess.run(  # noqa: S603 - fixed executable, arguments built here
        [git, "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ProvenanceError(f"git {' '.join(args)} failed: {result.stderr.strip()}")


def reproduce(root: Path, trial: TrialRecord, invoke: Invoke = _uv_invoke) -> list[str]:
    """Rerun ``trial`` from a blank state at its recorded commit; return the differences."""
    manifest = RunManifest.from_dict(trial.manifest)
    study = trial.config.get("study")
    if not isinstance(study, str) or manifest.clock_limit is None:
        return [f"{trial.trial_id}: the ledger entry does not say how to rerun it"]
    with tempfile.TemporaryDirectory(prefix="finresearch-reproduce-") as tmp:
        worktree, out = Path(tmp) / "code", Path(tmp) / "run.json"
        _git(root, "worktree", "add", "--detach", "--quiet", str(worktree), manifest.code.commit)
        try:
            invoke(
                worktree,
                [
                    "run",
                    "--study",
                    study,
                    "--hypothesis",
                    trial.hypothesis,
                    "--segment",
                    str(trial.segment),
                    "--now",
                    format_utc(manifest.clock_limit),
                    "--root",
                    str(worktree),
                    "--json-out",
                    str(out),
                ],
            )
            got: dict[str, Any] = json.loads(out.read_bytes())
        finally:
            _git(root, "worktree", "remove", "--force", str(worktree))
    problems: list[str] = []
    for key in ("config", "result_fingerprint", "output_fingerprint"):
        if got.get(key) != getattr(trial, key):
            problems.append(f"{trial.trial_id}: {key} differs on rerun")
    return problems
