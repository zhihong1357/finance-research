"""Schemas for registry records. Each class validates one kind of record."""

import re
from datetime import date, datetime
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from finresearch.core.config import load_toml
from finresearch.core.fingerprint import Fingerprint, digest_value
from finresearch.core.provenance import RunManifest
from finresearch.core.time import ensure_utc, format_utc


def _utc(t: datetime) -> datetime:
    return ensure_utc(t)


def _fingerprint_text(s: str) -> str:
    Fingerprint.parse(s)
    return s


UtcDatetime = Annotated[
    datetime, AfterValidator(_utc), PlainSerializer(format_utc, return_type=str, when_used="json")
]
FingerprintText = Annotated[str, AfterValidator(_fingerprint_text)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

HYPOTHESIS_ID = re.compile(r"H-\d{4}")
TRIAL_ID = re.compile(r"T-\d{6}")
DECISION_ID = re.compile(r"D-\d{4}")
RELATION_ID = re.compile(r"R-\d{4}")
REFUSAL_ID = re.compile(r"Q-\d{6}")
COMMIT = re.compile(r"[0-9a-f]{40}")
SOURCE_ID = re.compile(r"[a-z0-9]+(\.[a-z0-9_]+)+")

HypothesisId = Annotated[str, StringConstraints(pattern=HYPOTHESIS_ID.pattern)]
TrialId = Annotated[str, StringConstraints(pattern=TRIAL_ID.pattern)]
DecisionId = Annotated[str, StringConstraints(pattern=DECISION_ID.pattern)]
RelationId = Annotated[str, StringConstraints(pattern=RELATION_ID.pattern)]
RefusalId = Annotated[str, StringConstraints(pattern=REFUSAL_ID.pattern)]
CommitId = Annotated[str, StringConstraints(pattern=COMMIT.pattern)]
SourceId = Annotated[str, StringConstraints(pattern=SOURCE_ID.pattern)]


class Strict(BaseModel):
    """Unknown fields are errors: a misspelt field must not be silently ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- hypothesis cards


class Program(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"


class Universe(StrEnum):
    U1 = "U1"
    U2 = "U2"
    MACRO = "macro"
    SYNTHETIC = "synthetic"


class ReturnSource(StrEnum):
    """The six sources of return in docs/disciplines.md section 3, or unknown (principle 2)."""

    RISK_PREMIUM = "risk_premium"
    LIQUIDITY_PROVISION = "liquidity_provision"
    STRUCTURAL_FLOW = "structural_flow"
    BEHAVIORAL = "behavioral"
    INFORMATION = "information"
    ARBITRAGE = "arbitrage"
    UNKNOWN = "unknown"
    NONE = "none"  # meta-research (P0): the subject is the research process, not a market


class Output(StrEnum):
    RETURN = "return"  # enters the combined model
    VOLATILITY = "volatility"  # enters the risk model
    NONE = "none"  # meta-research and diagnostics


class Status(StrEnum):
    DRAFT = "draft"
    EXPLORING = "exploring"
    REGISTERED = "registered"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


TERMINAL = frozenset({Status.CONFIRMED, Status.REJECTED, Status.INCONCLUSIVE})
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.DRAFT: frozenset({Status.EXPLORING, Status.REGISTERED, Status.REJECTED}),
    Status.EXPLORING: frozenset({Status.REGISTERED, Status.REJECTED}),
    Status.REGISTERED: TERMINAL,
}


class Scope(Strict):
    universe: Universe
    instruments: Text
    horizon: Text
    output: Output


class DateRange(Strict):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"range ends ({self.end}) before it starts ({self.start})")
        return self


def _thresholds() -> dict[str, float]:
    return {k: float(v) for k, v in load_toml("protocol")["thresholds"].items()}


class Plan(Strict):
    """The confirmatory test. Frozen once the hypothesis is registered."""

    exploration: DateRange | None = None
    confirmation: DateRange
    vault: DateRange
    metric: Text
    test: Text
    threshold_t: float = Field(default_factory=lambda: _thresholds()["t_default"])
    threshold_reason: str = ""
    dsr_min: float = Field(default_factory=lambda: _thresholds()["dsr_min"])
    pbo_max: float = Field(default_factory=lambda: _thresholds()["pbo_max"])
    baseline_model: str = ""
    controls: list[Text] = Field(default_factory=lambda: list[str]())
    robustness: list[Text] = Field(default_factory=lambda: list[str]())

    @model_validator(mode="after")
    def _protocol(self) -> Self:
        th = _thresholds()
        if self.threshold_t < th["t_floor"]:
            raise ValueError(f"threshold_t {self.threshold_t} is below the floor {th['t_floor']}")
        if self.threshold_t < th["t_default"] and not self.threshold_reason.strip():
            raise ValueError(f"threshold_t below {th['t_default']} needs threshold_reason")
        if self.dsr_min < th["dsr_min"]:
            raise ValueError(f"dsr_min {self.dsr_min} is looser than {th['dsr_min']}")
        if self.pbo_max > th["pbo_max"]:
            raise ValueError(f"pbo_max {self.pbo_max} is looser than {th['pbo_max']}")
        segments = [s for s in (self.exploration, self.confirmation, self.vault) if s is not None]
        for earlier, later in pairwise(segments):
            if not earlier.end < later.start:
                raise ValueError(
                    "exploration, confirmation and vault must be in order and not overlap"
                )
        return self


class HistoryEntry(Strict):
    at: UtcDatetime
    status: Status
    note: str = ""
    plan_fingerprint: FingerprintText | None = None


class HypothesisCard(Strict):
    id: HypothesisId
    title: Text
    program: Program
    return_source: ReturnSource
    scope: Scope
    mechanism: Text
    counterparty: Text
    why_persistent: Text
    why_us: Text
    falsification: Text
    prior: float = Field(ge=0.0, le=1.0)
    data: list[SourceId] = Field(default_factory=lambda: list[str]())
    power: Text
    plan: Plan
    history: list[HistoryEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _meta_research(self) -> Self:
        meta = self.program is Program.P0
        if meta != (self.return_source is ReturnSource.NONE) or meta != (
            self.scope.output is Output.NONE
        ):
            raise ValueError(
                "P0 (meta-research), and only P0, has return_source none and output none"
            )
        return self

    @model_validator(mode="after")
    def _history(self) -> Self:
        if self.history[0].status is not Status.DRAFT:
            raise ValueError("history must start with status draft")
        for prev, nxt in pairwise(self.history):
            if nxt.at < prev.at:
                raise ValueError("history must be in chronological order")
            if nxt.status not in TRANSITIONS.get(prev.status, frozenset()):
                raise ValueError(f"status cannot go from {prev.status} to {nxt.status}")
        for entry in self.history:
            if (entry.status is Status.REGISTERED) != (entry.plan_fingerprint is not None):
                raise ValueError(
                    "plan_fingerprint belongs on the registration entry and only there"
                )
        return self

    @property
    def status(self) -> Status:
        return self.history[-1].status

    @property
    def registered_at(self) -> datetime | None:
        return next((e.at for e in self.history if e.status is Status.REGISTERED), None)

    @property
    def registered_plan(self) -> str | None:
        return next((e.plan_fingerprint for e in self.history if e.plan_fingerprint), None)


def plan_fingerprint(raw_plan: dict[str, Any]) -> Fingerprint:
    """Fingerprint of the plan exactly as written in the card (not as defaulted by the model)."""
    return digest_value(raw_plan)


# ---------------------------------------------------------------- append-only logs


class Segment(StrEnum):
    EXPLORATION = "exploration"
    CONFIRMATION = "confirmation"
    VAULT = "vault"
    FORWARD = "forward"
    SYNTHETIC = "synthetic"


class TrialRecord(Strict):
    trial_id: TrialId
    recorded_at: UtcDatetime
    hypothesis: HypothesisId
    segment: Segment
    manifest: dict[str, Any]
    config: dict[str, Any]
    result_fingerprint: FingerprintText
    output_fingerprint: FingerprintText
    metrics: dict[str, float]
    vault_approval: DecisionId | None = None
    prev: FingerprintText | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        manifest = RunManifest.from_dict(self.manifest)
        if not manifest.reproducible:
            raise ValueError("a run from a working tree with local changes cannot enter the ledger")
        if str(manifest.fingerprint) != self.result_fingerprint:
            raise ValueError("result_fingerprint does not match the manifest")
        if digest_value(self.config) != manifest.config:
            raise ValueError("config does not match the manifest's config fingerprint")
        if (self.segment is Segment.VAULT) != (self.vault_approval is not None):
            raise ValueError("vault trials, and only vault trials, cite the owner's approval")
        return self


class RefusalKind(StrEnum):
    LOOKAHEAD = "lookahead"  # a query past the clock limit
    VAULT_SEALED = "vault_sealed"  # the vault without the owner's approval
    NOT_REGISTERED = "not_registered"  # held-out data for an unregistered hypothesis


class Refusal(Strict):
    """A request the point-in-time door refused. It disclosed nothing; it is kept as a record."""

    refusal_id: RefusalId
    recorded_at: UtcDatetime
    hypothesis: HypothesisId
    segment: Segment
    study: Text
    kind: RefusalKind
    detail: Text
    commit: CommitId
    dirty: bool
    prev: FingerprintText | None


class DecisionKind(StrEnum):
    VAULT_ACCESS = "vault_access"
    GATE = "gate"
    ALLOCATION = "allocation"
    OTHER = "other"


class OwnerDecision(Strict):
    decision_id: DecisionId
    recorded_at: UtcDatetime
    kind: DecisionKind
    subject: str = ""
    summary: Text
    basis: list[str] = Field(default_factory=lambda: list[str]())
    prev: FingerprintText | None


# ---------------------------------------------------------------- catalogues


class RelationStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class Relation(Strict):
    id: RelationId
    title: Text
    instruments: list[Text] = Field(min_length=2)
    formula: Text
    cost_model: Text
    why_gap_exists: Text
    status: RelationStatus = RelationStatus.DRAFT


class Access(StrEnum):
    ARCHIVE = "archive"  # long-lived files that can be downloaded again
    SNAPSHOT = "snapshot"  # current state only: lost if not recorded
    API = "api"  # queryable history


class Source(Strict):
    id: SourceId
    name: Text
    url: Text
    access: Access
    redownloadable: bool
    official_checksums: bool = False
    event_time: Text
    available_at: Text
    rate_limit: str = ""
    terms_url: str = ""
    terms_checked: bool = False
    known_issues: list[Text] = Field(default_factory=lambda: list[str]())

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.access is Access.SNAPSHOT and self.redownloadable:
            raise ValueError("a snapshot source cannot be downloaded again")
        return self
