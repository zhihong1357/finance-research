"""Append-only, hash-chained JSON Lines files: trials, owner decisions, refusals.

Every line is the canonical JSON of one record (``core.fingerprint``) and
carries ``prev``: the fingerprint of the line before it (``null`` on the first
line). Editing, reordering or deleting any earlier line breaks the chain, and
``read_chain`` refuses the file. Rewriting the whole chain consistently is
caught separately, by comparing with git history (``registry.appendonly``).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from finresearch.core.fingerprint import Fingerprint, canonical_json, digest_bytes
from finresearch.core.provenance import RunManifest
from finresearch.core.time import ensure_utc
from finresearch.registry.models import (
    DecisionKind,
    OwnerDecision,
    Refusal,
    RefusalKind,
    Segment,
    TrialRecord,
)

_NEWLINE = b"\n"


class ChainError(ValueError):
    """An append-only file has been altered or is malformed."""


def read_chain(path: Path) -> list[dict[str, Any]]:
    """All records, after verifying canonical form and the hash chain."""
    if not path.exists():
        return []
    data = path.read_bytes()
    if not data:
        return []
    if not data.endswith(_NEWLINE):
        raise ChainError(f"{path}: the last line is not terminated")
    records: list[dict[str, Any]] = []
    prev: Fingerprint | None = None
    for n, line in enumerate(data.split(_NEWLINE)[:-1], start=1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChainError(f"{path}:{n}: not JSON ({exc})") from exc
        if not isinstance(obj, dict):
            raise ChainError(f"{path}:{n}: each line must be a JSON object")
        record: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
        if canonical_json(record) != line:
            raise ChainError(f"{path}:{n}: not in canonical form (was the line edited by hand?)")
        expected = None if prev is None else str(prev)
        if record.get("prev") != expected:
            raise ChainError(
                f"{path}:{n}: hash chain broken (an earlier line was changed or removed)"
            )
        prev = digest_bytes(line)
        records.append(record)
    return records


def _head(existing: list[dict[str, Any]]) -> str | None:
    return None if not existing else str(digest_bytes(canonical_json(existing[-1])))


def _append(path: Path, record: dict[str, Any]) -> None:
    line = canonical_json(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(line + _NEWLINE)


def append_trial(
    path: Path,
    *,
    hypothesis: str,
    segment: Segment,
    manifest: RunManifest,
    config: dict[str, Any],
    metrics: dict[str, float],
    output_fingerprint: Fingerprint,
    recorded_at: datetime,
    vault_approval: str | None = None,
) -> TrialRecord:
    """Validate and append one trial; returns the record as written."""
    existing = read_chain(path)
    record = TrialRecord(
        trial_id=f"T-{len(existing) + 1:06d}",
        recorded_at=ensure_utc(recorded_at),
        hypothesis=hypothesis,
        segment=segment,
        manifest=manifest.to_dict(),
        config=config,
        result_fingerprint=str(manifest.fingerprint),
        output_fingerprint=str(output_fingerprint),
        metrics=metrics,
        vault_approval=vault_approval,
        prev=_head(existing),
    )
    _append(path, record.model_dump(mode="json"))
    return record


def append_owner_decision(
    path: Path,
    *,
    kind: DecisionKind,
    summary: str,
    recorded_at: datetime,
    subject: str = "",
    basis: list[str] | None = None,
) -> OwnerDecision:
    """Validate and append one owner decision; returns the record as written."""
    existing = read_chain(path)
    record = OwnerDecision(
        decision_id=f"D-{len(existing) + 1:04d}",
        recorded_at=ensure_utc(recorded_at),
        kind=kind,
        subject=subject,
        summary=summary,
        basis=basis or [],
        prev=_head(existing),
    )
    _append(path, record.model_dump(mode="json"))
    return record


def append_refusal(
    path: Path,
    *,
    hypothesis: str,
    segment: Segment,
    study: str,
    kind: RefusalKind,
    detail: str,
    commit: str,
    dirty: bool,
    recorded_at: datetime,
) -> Refusal:
    """Validate and append one refusal; returns the record as written."""
    existing = read_chain(path)
    record = Refusal(
        refusal_id=f"Q-{len(existing) + 1:06d}",
        recorded_at=ensure_utc(recorded_at),
        hypothesis=hypothesis,
        segment=segment,
        study=study,
        kind=kind,
        detail=detail,
        commit=commit,
        dirty=dirty,
        prev=_head(existing),
    )
    _append(path, record.model_dump(mode="json"))
    return record
