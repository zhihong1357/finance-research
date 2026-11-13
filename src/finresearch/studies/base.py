"""What a study is: named inputs, and a computation that sees data only through the door."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from finresearch.data.pit import PointInTime

# Sections of the standard report (docs/protocol.md section 8) that a study writes. The run
# framework writes the other two from the registry: 2 (hypothesis) and 12 (run manifest).
STUDY_SECTIONS = frozenset({1, 3, 4, 5, 6, 7, 8, 9, 10, 11})


@dataclass(frozen=True, slots=True)
class StudyResult:
    metrics: dict[str, float]
    sections: dict[int, str]


@dataclass(frozen=True, slots=True)
class Study:
    """``sources`` must be listed in the hypothesis card's ``data``; ``config`` names a file
    under ``config/studies/``."""

    name: str
    sources: tuple[str, ...]
    compute: Callable[[PointInTime, Mapping[str, Any]], StudyResult]
