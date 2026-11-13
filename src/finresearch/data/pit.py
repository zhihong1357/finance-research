"""The point-in-time door: the only way research code reads data.

A run gets a ``PointInTime`` built around a ``Clock``. Every query names an
instant ``t`` and receives what was public at ``t`` - nothing later, and for
revised data the version that was current then. Two limits are enforced here,
in code, rather than left to discipline:

* **Clock limit.** Asking about any instant after ``clock.limit`` raises
  ``LookaheadError``. An exploration run is limited to the end of its
  exploration segment, a confirmation run to the end of the confirmation
  segment, and so on (``clock_for``).
* **The vault.** A clock that reaches past the start of the vault cannot even
  be created without a ``VaultToken``, and a token is only issued against the
  owner's ``vault_access`` decision for that hypothesis in
  ``registry/owner_decisions.jsonl``.

Research code never holds a table. The run framework opens the door over a
set of tables and hands the study only the door; the study asks for tables by
name. For a decision taken at instant ``t`` the study works inside
``pit.view(t)``, a door whose clock stops at ``t``: anything published later is
not merely discouraged but unreachable.

The door also records which tables were read, with their fingerprints, so the
run's provenance (``RunManifest.data``) is filled in by construction.

Every refusal is an ``AccessRefusedError``. A refused query discloses nothing, and
the run framework writes each one to ``registry/refusals.jsonl``.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from finresearch.core.fingerprint import Fingerprint
from finresearch.core.time import ensure_utc
from finresearch.data.table import PitTable, Snapshot, versions_as_of
from finresearch.registry.ledger import read_chain
from finresearch.registry.models import DecisionKind, HypothesisCard, OwnerDecision, Segment, Status
from finresearch.registry.validate import OWNER_DECISIONS

_ONE_MICROSECOND = timedelta(microseconds=1)


class AccessRefusedError(RuntimeError):
    """The door refused a request. Nothing was disclosed."""


class LookaheadError(AccessRefusedError):
    """A query asked about an instant after the clock limit."""


class VaultSealedError(AccessRefusedError):
    """The vault was reached without the owner's approval."""


class NotRegisteredError(AccessRefusedError):
    """Confirmation, vault or forward data was requested for an unregistered hypothesis."""


def start_of_day(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=UTC)


def end_of_day(d: date) -> datetime:
    """The last representable instant of ``d`` (UTC): inclusive segment ends."""
    return start_of_day(d + timedelta(days=1)) - _ONE_MICROSECOND


@dataclass(frozen=True, slots=True)
class VaultToken:
    """Proof of the owner's approval to open one hypothesis's vault."""

    decision_id: str
    hypothesis: str


def issue_vault_token(root: Path, hypothesis: str) -> VaultToken:
    """A token backed by the owner's latest ``vault_access`` decision for ``hypothesis``."""
    decisions = [OwnerDecision.model_validate(r) for r in read_chain(root / OWNER_DECISIONS)]
    approvals = [
        d for d in decisions if d.kind is DecisionKind.VAULT_ACCESS and d.subject == hypothesis
    ]
    if not approvals:
        raise VaultSealedError(f"the owner has not approved opening the vault for {hypothesis}")
    return VaultToken(approvals[-1].decision_id, hypothesis)


@dataclass(frozen=True, slots=True)
class Clock:
    limit: datetime
    vault_start: datetime | None = None
    token: VaultToken | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "limit", ensure_utc(self.limit))
        if self.vault_start is not None:
            object.__setattr__(self, "vault_start", ensure_utc(self.vault_start))
            if self.limit >= self.vault_start and self.token is None:
                raise VaultSealedError(
                    f"a clock reaching {self.limit} passes the vault start {self.vault_start}"
                )


def clock_for(
    card: HypothesisCard, segment: Segment, now: datetime, token: VaultToken | None = None
) -> Clock:
    """The clock a run of ``card`` on ``segment`` is allowed (docs/protocol.md section 3)."""
    plan = card.plan
    vault_start = start_of_day(plan.vault.start)
    if segment is Segment.SYNTHETIC:
        return Clock(limit=ensure_utc(now))
    if segment is Segment.EXPLORATION:
        if plan.exploration is None:
            raise ValueError(f"{card.id} has no exploration segment")
        return Clock(limit=end_of_day(plan.exploration.end), vault_start=vault_start)
    if card.status is not Status.REGISTERED:
        raise NotRegisteredError(
            f"{segment} runs need {card.id} to be registered (it is {card.status})"
        )
    if segment is Segment.CONFIRMATION:
        return Clock(limit=end_of_day(plan.confirmation.end), vault_start=vault_start)
    if token is None or token.hypothesis != card.id:
        raise VaultSealedError(f"{segment} runs of {card.id} need a vault token for {card.id}")
    limit = end_of_day(plan.vault.end) if segment is Segment.VAULT else ensure_utc(now)
    return Clock(limit=limit, vault_start=vault_start, token=token)


@dataclass
class _Trace:
    """Shared by a door and every view of it, so the run's provenance sees all reads."""

    used: dict[str, Fingerprint] = field(default_factory=lambda: dict[str, Fingerprint]())
    vault_crossed: bool = False


class PointInTime:
    """What could be known, and when.

    Hand one of these to research code, never a table or a path.
    """

    def __init__(
        self, clock: Clock, tables: Iterable[PitTable], *, _trace: _Trace | None = None
    ) -> None:
        self._clock = clock
        self._tables: dict[str, PitTable] = {}
        for t in tables:
            if t.name in self._tables:
                raise ValueError(f"two tables named {t.name!r} behind one door")
            self._tables[t.name] = t
        self._trace = _trace or _Trace()

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def names(self) -> list[str]:
        return sorted(self._tables)

    @property
    def used(self) -> dict[str, Fingerprint]:
        """Fingerprint of every table read so far, by name: the run's ``RunManifest.data``."""
        return dict(sorted(self._trace.used.items()))

    @property
    def vault_crossed(self) -> bool:
        return self._trace.vault_crossed

    def _check(self, t: datetime) -> datetime:
        t = ensure_utc(t)
        if t > self._clock.limit:
            raise LookaheadError(
                f"asked about {t.isoformat()}, but this door may only see up to "
                f"{self._clock.limit.isoformat()}"
            )
        vault_start = self._clock.vault_start
        if vault_start is not None and t >= vault_start:
            if self._clock.token is None:
                raise VaultSealedError(f"{t.isoformat()} is inside the sealed vault")
            self._trace.vault_crossed = True
        return t

    def as_of(self, name: str, t: datetime) -> Snapshot:
        """Every observation of ``name`` public at ``t``, each in the version current at ``t``."""
        t = self._check(t)
        table = self._tables.get(name)
        if table is None:
            raise KeyError(f"no table {name!r} behind this door (have {self.names})")
        self._trace.used[name] = table.fingerprint
        return Snapshot(name, t, versions_as_of(table, t))

    def latest(self, name: str) -> Snapshot:
        """``as_of`` the clock limit: everything this door may see."""
        return self.as_of(name, self._clock.limit)

    def view(self, t: datetime) -> PointInTime:
        """The same tables behind a clock that stops at ``t``: the door for a decision at ``t``."""
        t = self._check(t)
        clock = Clock(limit=t, vault_start=self._clock.vault_start, token=self._clock.token)
        return PointInTime(clock, self._tables.values(), _trace=self._trace)
