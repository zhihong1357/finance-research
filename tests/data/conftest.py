"""A tiny point-in-time dataset with a revision, built in code."""

from datetime import datetime, timedelta

import pyarrow as pa
import pytest

from finresearch.core.time import utc
from finresearch.data.table import TIME_TYPE, PitTable

# (series, event_time, available_at, value): September CPI is revised on 13 November
ROWS: list[tuple[str, datetime, datetime, float]] = [
    ("CPI", utc(2026, 9, 30), utc(2026, 10, 15, 12, 30), 3.0),
    ("CPI", utc(2026, 9, 30), utc(2026, 11, 13, 13, 30), 3.1),
    ("CPI", utc(2026, 10, 31), utc(2026, 11, 13, 13, 30), 2.9),
    ("PPI", utc(2026, 9, 30), utc(2026, 10, 16, 12, 30), 1.0),
]
INGEST_DELAY = timedelta(hours=1)


def make_table(
    rows: list[tuple[str, datetime, datetime, float]] = ROWS, name: str = "macro"
) -> PitTable:
    return PitTable(
        name,
        ("series",),
        pa.table(
            {
                "series": [r[0] for r in rows],
                "event_time": pa.array([r[1] for r in rows], type=TIME_TYPE),
                "available_at": pa.array([r[2] for r in rows], type=TIME_TYPE),
                "ingested_at": pa.array([r[2] + INGEST_DELAY for r in rows], type=TIME_TYPE),
                "value": [r[3] for r in rows],
            }
        ),
    )


@pytest.fixture
def macro() -> PitTable:
    return make_table()
