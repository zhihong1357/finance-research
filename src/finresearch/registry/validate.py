"""Load the whole registry and check it, including the protocol rules that span files.

``check(root)`` returns a list of problems; an empty list means the registry is
consistent. Each problem names the file it is about.
"""

import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from finresearch.core.provenance import RunManifest
from finresearch.registry.ledger import ChainError, read_chain
from finresearch.registry.models import (
    DecisionKind,
    HypothesisCard,
    OwnerDecision,
    Relation,
    Segment,
    Source,
    Status,
    TrialRecord,
    plan_fingerprint,
)

HYPOTHESES = Path("registry/hypotheses")
RELATIONS = Path("registry/relations")
SOURCES = Path("registry/sources")
TRIALS = Path("registry/trials.jsonl")
OWNER_DECISIONS = Path("registry/owner_decisions.jsonl")
APPEND_ONLY = (TRIALS, OWNER_DECISIONS)


@dataclass
class Card:
    card: HypothesisCard
    raw: dict[str, Any]
    path: Path


@dataclass
class Registry:
    hypotheses: dict[str, Card] = field(default_factory=lambda: dict[str, Card]())
    relations: dict[str, Relation] = field(default_factory=lambda: dict[str, Relation]())
    sources: dict[str, Source] = field(default_factory=lambda: dict[str, Source]())
    trials: list[TrialRecord] = field(default_factory=lambda: list[TrialRecord]())
    decisions: list[OwnerDecision] = field(default_factory=lambda: list[OwnerDecision]())


def _errors(where: Path, exc: ValidationError) -> list[str]:
    return [
        f"{where}: {'.'.join(str(p) for p in e['loc']) or '<record>'}: {e['msg']}"
        for e in exc.errors()
    ]


def _load_toml_dir[M: BaseModel](
    root: Path, rel: Path, model: type[M], problems: list[str]
) -> list[tuple[M, dict[str, Any], Path]]:
    out: list[tuple[M, dict[str, Any], Path]] = []
    for path in sorted((root / rel).glob("*.toml")):
        shown = path.relative_to(root)
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            obj = model.model_validate(raw)
        except tomllib.TOMLDecodeError as exc:
            problems.append(f"{shown}: not valid TOML ({exc})")
            continue
        except ValidationError as exc:
            problems.extend(_errors(shown, exc))
            continue
        if getattr(obj, "id", None) != path.stem:
            problems.append(f"{shown}: file name must be the id ({getattr(obj, 'id', None)}.toml)")
            continue
        out.append((obj, raw, shown))
    return out


def _load_chain[M: BaseModel](
    root: Path, rel: Path, model: type[M], problems: list[str]
) -> list[M]:
    try:
        rows = read_chain(root / rel)
    except ChainError as exc:
        problems.append(str(exc).replace(str(root) + "/", ""))
        return []
    out: list[M] = []
    for n, row in enumerate(rows, start=1):
        try:
            out.append(model.model_validate(row))
        except ValidationError as exc:
            problems.extend(_errors(Path(f"{rel}:{n}"), exc))
    return out


def load(root: Path) -> tuple[Registry, list[str]]:
    problems: list[str] = []
    reg = Registry()
    for card, raw, path in _load_toml_dir(root, HYPOTHESES, HypothesisCard, problems):
        reg.hypotheses[card.id] = Card(card, raw, path)
    reg.relations = {r.id: r for r, _, _ in _load_toml_dir(root, RELATIONS, Relation, problems)}
    reg.sources = {s.id: s for s, _, _ in _load_toml_dir(root, SOURCES, Source, problems)}
    reg.trials = _load_chain(root, TRIALS, TrialRecord, problems)
    reg.decisions = _load_chain(root, OWNER_DECISIONS, OwnerDecision, problems)
    return reg, problems


def _check_sequence(
    rel: Path, ids: list[str], times: list[Any], prefix: str, width: int
) -> list[str]:
    problems: list[str] = []
    for n, (rid, expected) in enumerate(zip(ids, range(1, len(ids) + 1), strict=True), start=1):
        if rid != f"{prefix}-{expected:0{width}d}":
            problems.append(f"{rel}:{n}: id {rid} out of sequence")
    problems.extend(
        f"{rel}:{n}: recorded_at goes backwards"
        for n in range(2, len(times) + 1)
        if times[n - 1] < times[n - 2]
    )
    return problems


def _check_cards(reg: Registry) -> list[str]:
    problems: list[str] = []
    for c in reg.hypotheses.values():
        frozen = c.card.registered_plan
        if frozen is not None and str(plan_fingerprint(c.raw["plan"])) != frozen:
            problems.append(f"{c.path}: the plan was changed after registration")
        problems.extend(
            f"{c.path}: unknown data source {s}" for s in c.card.data if s not in reg.sources
        )
    return problems


def _check_trial(
    t: TrialRecord, n: int, reg: Registry, decisions: dict[str, OwnerDecision]
) -> list[str]:
    where = f"{TRIALS}:{n}"
    c = reg.hypotheses.get(t.hypothesis)
    if c is None:
        return [f"{where}: unknown hypothesis {t.hypothesis}"]
    card, problems = c.card, list[str]()
    registered = card.registered_at
    clock = RunManifest.from_dict(t.manifest).clock_limit
    bounds = {
        Segment.EXPLORATION: card.plan.exploration,
        Segment.CONFIRMATION: card.plan.confirmation,
        Segment.VAULT: card.plan.vault,
    }
    if t.segment in bounds:
        segment = bounds[t.segment]
        if segment is None:
            problems.append(f"{where}: {t.hypothesis} has no {t.segment} segment")
        elif clock is None or clock.date() > segment.end:
            problems.append(
                f"{where}: clock limit passes the end of the {t.segment} segment ({segment.end})"
            )
    if t.segment in {Segment.CONFIRMATION, Segment.VAULT, Segment.FORWARD} and (
        registered is None or registered > t.recorded_at
    ):
        problems.append(f"{where}: {t.segment} trial before {t.hypothesis} was registered")
    if t.segment is Segment.VAULT:
        d = decisions.get(t.vault_approval or "")
        if d is None or d.kind is not DecisionKind.VAULT_ACCESS or d.subject != t.hypothesis:
            problems.append(
                f"{where}: vault trial without the owner's vault_access decision for {t.hypothesis}"
            )
        elif d.recorded_at > t.recorded_at:
            problems.append(f"{where}: vault trial recorded before the owner's approval")
    return problems


def _check_trials(reg: Registry) -> list[str]:
    decisions = {d.decision_id: d for d in reg.decisions}
    problems: list[str] = []
    for n, t in enumerate(reg.trials, start=1):
        problems.extend(_check_trial(t, n, reg, decisions))
    confirmations = Counter(t.hypothesis for t in reg.trials if t.segment is Segment.CONFIRMATION)
    problems.extend(
        f"{TRIALS}: {h} has {k} confirmation trials; the protocol allows one"
        for h, k in confirmations.items()
        if k > 1
    )
    for c in reg.hypotheses.values():
        if (
            c.card.status in {Status.CONFIRMED, Status.INCONCLUSIVE}
            and confirmations[c.card.id] == 0
        ):
            problems.append(
                f"{c.path}: status {c.card.status} needs a confirmation trial in the ledger"
            )
    for d in reg.decisions:
        if d.kind is DecisionKind.VAULT_ACCESS and d.subject not in reg.hypotheses:
            problems.append(
                f"{OWNER_DECISIONS}: {d.decision_id} grants vault access to unknown {d.subject!r}"
            )
    return problems


def check(root: Path) -> list[str]:
    reg, problems = load(root)
    problems += _check_sequence(
        TRIALS, [t.trial_id for t in reg.trials], [t.recorded_at for t in reg.trials], "T", 6
    )
    problems += _check_sequence(
        OWNER_DECISIONS,
        [d.decision_id for d in reg.decisions],
        [d.recorded_at for d in reg.decisions],
        "D",
        4,
    )
    problems += _check_cards(reg)
    problems += _check_trials(reg)
    return problems
