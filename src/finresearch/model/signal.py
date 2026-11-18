"""The signal interface (docs/architecture.md section 5.1).

A signal is what research produces (decision 0006): for each instrument of a
research universe, at each decision instant, a standardised score that predicts
returns over a stated horizon. It has a declaration (``SignalSpec``) and a
computation (``Signal.compute``).

``evaluate`` is the only way a signal is run. For a decision at ``at`` it hands
the computation ``door.view(at)``: a door whose clock stops at ``at``. Anything
published later is unreachable, so a score at ``at`` can only use what was
public at ``at``, and the scores are known at ``at``. A computation that asks
about a later instant gets ``LookaheadError`` and produces no scores. Reads go
through the same provenance record as the outer door's.

As with studies, the run framework puts behind the door only the tables the
hypothesis card lists, and ``tests/test_architecture.py`` keeps concrete
signals (``finresearch.signals``) from reaching data any other way.

``standardize`` turns raw values into cross-sectional z-scores (mean 0,
population standard deviation 1); a computation may use it to put its output on
the standard scale.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from finresearch.core.time import ensure_utc
from finresearch.data.pit import PointInTime
from finresearch.registry.models import HYPOTHESIS_ID, Universe

_MIN_FOR_DISPERSION = 2


class SignalError(ValueError):
    """A signal's declaration or output is not valid."""


@dataclass(frozen=True, slots=True)
class SignalSpec:
    name: str
    version: int  # any change to a signal makes a new version
    hypothesis: str  # the hypothesis card it comes from
    universe: Universe
    horizon: timedelta  # how far ahead the score predicts returns
    sources: tuple[str, ...]  # data sources it reads; must be on the card

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise SignalError(f"name must be letters, digits and underscores, got {self.name!r}")
        if self.version < 1:
            raise SignalError(f"version starts at 1, got {self.version}")
        if not HYPOTHESIS_ID.fullmatch(self.hypothesis):
            raise SignalError(f"hypothesis must look like H-0001, got {self.hypothesis!r}")
        if self.horizon <= timedelta(0):
            raise SignalError(f"horizon must be positive, got {self.horizon}")
        if not self.sources:
            raise SignalError("a signal reads at least one data source")

    @property
    def key(self) -> str:
        return f"{self.name}@v{self.version}"


Compute = Callable[[PointInTime, Mapping[str, Any]], Mapping[str, float]]


@dataclass(frozen=True, slots=True)
class Signal:
    spec: SignalSpec
    compute: Compute  # (door stopped at the decision instant, config) -> instrument: score


@dataclass(frozen=True, slots=True)
class Scores:
    signal: str  # SignalSpec.key
    at: datetime  # the decision instant; the scores are known from here on
    horizon: timedelta
    values: Mapping[str, float]  # instrument -> standardised score, sorted by instrument


def evaluate(signal: Signal, door: PointInTime, at: datetime, config: Mapping[str, Any]) -> Scores:
    """Run ``signal`` for a decision at ``at``, seeing only what was public at ``at``."""
    at = ensure_utc(at)
    raw = signal.compute(door.view(at), config)
    for instrument, value in raw.items():
        if not instrument:
            raise SignalError(f"{signal.spec.key}: an instrument has an empty name")
        if not math.isfinite(value):
            raise SignalError(f"{signal.spec.key}: score for {instrument} is {value}")
    values = {k: float(raw[k]) for k in sorted(raw)}
    return Scores(signal.spec.key, at, signal.spec.horizon, values)


def standardize(raw: Mapping[str, float]) -> dict[str, float]:
    """Cross-sectional z-scores; all zero when there is no dispersion to scale by."""
    if any(not math.isfinite(v) for v in raw.values()):
        raise SignalError("values must be finite")
    n = len(raw)
    if n < _MIN_FOR_DISPERSION:
        return dict.fromkeys(raw, 0.0)
    mean = math.fsum(raw.values()) / n
    sd = math.sqrt(math.fsum((v - mean) ** 2 for v in raw.values()) / n)
    if sd == 0.0:
        return dict.fromkeys(raw, 0.0)
    return {k: (v - mean) / sd for k, v in raw.items()}
