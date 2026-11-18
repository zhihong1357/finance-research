"""The verdict: protocol section 5's seven checks, in one place.

A confirmation run hands over its statistics as ``Evidence``; ``judge`` applies
the thresholds and says which checks pass and whether all of them do. The
meta-research (Phase 3) measures the false positive rate of exactly this
function, and the run framework will use it too (step 3.13), so there is only
one implementation of "does this result count".

Every effect is measured in the direction the hypothesis predicts: positive
supports it, so "still positive" reads the same for a return signal (P&L) and
for a volatility signal (the predicted difference).

| check | passes when | not applicable when |
|---|---|---|
| ``T_STAT`` | ``t >= threshold_t`` | never |
| ``DSR`` | ``dsr >= dsr_min`` | never |
| ``PBO`` | ``pbo <= pbo_max`` | no selection among parameter sets (``pbo`` is None) |
| ``COST_DOUBLED`` | the effect with costs doubled is above 0 | the signal trades nothing itself |
| ``WITHOUT_BEST_TWO`` | the effect without the best 2 trades is above 0 | as above |
| ``PERTURBATION`` | every effect with parameters moved 20% is above 0 | no parameters |
| ``SEGMENTS`` | more than half of the sub-periods are above 0 | never |

A check that does not apply is reported as such, never silently passed: the
standard report lists all seven. ``passed`` is true when every check that
applies passes.

Thresholds come from the hypothesis card (``Thresholds.from_plan``), which
already refuses values looser than ``config/protocol.toml``; ``Thresholds``
itself accepts any values, because the meta-research also asks what a looser
or stricter bar would have done.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from finresearch.core.config import load_toml
from finresearch.registry.models import Plan

_MIN_SEGMENTS = 2


class VerdictError(ValueError):
    """The evidence or the thresholds are not usable."""


class Check(StrEnum):
    T_STAT = "t_stat"
    DSR = "dsr"
    PBO = "pbo"
    COST_DOUBLED = "cost_doubled"
    WITHOUT_BEST_TWO = "without_best_two"
    PERTURBATION = "perturbation"
    SEGMENTS = "segments"


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Thresholds:
    t: float
    dsr_min: float
    pbo_max: float

    def __post_init__(self) -> None:
        if not self.t > 0.0:
            raise VerdictError(f"threshold t must be positive, got {self.t}")
        if not 0.0 < self.dsr_min < 1.0:
            raise VerdictError(f"dsr_min must be strictly between 0 and 1, got {self.dsr_min}")
        if not 0.0 < self.pbo_max < 1.0:
            raise VerdictError(f"pbo_max must be strictly between 0 and 1, got {self.pbo_max}")

    @classmethod
    def defaults(cls) -> Thresholds:
        """The defaults in ``config/protocol.toml``."""
        th = load_toml("protocol")["thresholds"]
        return cls(float(th["t_default"]), float(th["dsr_min"]), float(th["pbo_max"]))

    @classmethod
    def from_plan(cls, plan: Plan) -> Thresholds:
        """The thresholds a hypothesis card froze (never looser than the defaults)."""
        return cls(plan.threshold_t, plan.dsr_min, plan.pbo_max)


@dataclass(frozen=True)
class Evidence:
    t: float  # in the predicted direction
    dsr: float
    segments: Sequence[float]  # the effect in each sub-period
    pbo: float | None = None  # None: no selection among parameter sets
    cost_doubled: float | None = None  # None: the signal trades nothing itself
    without_best_two: float | None = None
    perturbed: Sequence[float] = ()  # empty: no parameters to perturb

    def __post_init__(self) -> None:
        if len(self.segments) < _MIN_SEGMENTS:
            raise VerdictError(
                f"need at least {_MIN_SEGMENTS} sub-periods, got {len(self.segments)}"
            )
        if (self.cost_doubled is None) != (self.without_best_two is None):
            raise VerdictError("cost_doubled and without_best_two apply together or not at all")
        if not 0.0 <= self.dsr <= 1.0:
            raise VerdictError(f"dsr is a probability, got {self.dsr}")
        if self.pbo is not None and not 0.0 <= self.pbo <= 1.0:
            raise VerdictError(f"pbo is a probability, got {self.pbo}")


@dataclass(frozen=True)
class Verdict:
    outcomes: dict[Check, Outcome]

    @property
    def passed(self) -> bool:
        return all(o is not Outcome.FAILED for o in self.outcomes.values())

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c, o in self.outcomes.items() if o is Outcome.FAILED)


def _outcome(applies: bool, ok: bool) -> Outcome:
    if not applies:
        return Outcome.NOT_APPLICABLE
    return Outcome.PASSED if ok else Outcome.FAILED


def judge(evidence: Evidence, thresholds: Thresholds) -> Verdict:
    """Apply protocol section 5 to ``evidence``."""
    e, th = evidence, thresholds
    trades = e.cost_doubled is not None
    positive = sum(1 for s in e.segments if s > 0.0)
    return Verdict(
        {
            Check.T_STAT: _outcome(True, e.t >= th.t),
            Check.DSR: _outcome(True, e.dsr >= th.dsr_min),
            Check.PBO: _outcome(e.pbo is not None, e.pbo is not None and e.pbo <= th.pbo_max),
            Check.COST_DOUBLED: _outcome(
                trades, e.cost_doubled is not None and e.cost_doubled > 0.0
            ),
            Check.WITHOUT_BEST_TWO: _outcome(
                trades, e.without_best_two is not None and e.without_best_two > 0.0
            ),
            Check.PERTURBATION: _outcome(bool(e.perturbed), all(p > 0.0 for p in e.perturbed)),
            Check.SEGMENTS: _outcome(True, 2 * positive > len(e.segments)),
        }
    )
