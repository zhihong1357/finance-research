import pyarrow as pa
import pytest

from finresearch.core.time import utc
from finresearch.data import mark
from finresearch.data.fixed import parse

T0 = utc(2026, 11, 11)
US0 = int(T0.timestamp()) * 1_000_000
MINUTE = 60_000_000  # microseconds
PUBLISHED = utc(2026, 11, 12, 1, 8)


def _table(starts: list[int], closes: list[str], samples: list[int] | None = None) -> mark.PitTable:
    return mark.marks(
        "binance",
        "AAAUSDT",
        pa.array(starts, pa.int64()),
        parse(closes),
        pa.array(samples if samples is not None else [60] * len(starts), pa.int64()),
        PUBLISHED,
    )


def test_a_mark_price_is_available_when_its_minute_ends() -> None:
    # The second minute has no samples: its price is the first minute's, carried forward.
    t = _table([US0, US0 + MINUTE], ["100.25000000", "100.25000000"], [60, 0])
    first, second = t.table.to_pylist()
    assert (t.name, t.keys) == ("u1.mark_1m", ("exchange", "symbol"))
    assert (first["event_time"], first["available_at"]) == (T0, utc(2026, 11, 11, 0, 1))
    assert (second["event_time"], second["available_at"]) == (
        utc(2026, 11, 11, 0, 1),
        utc(2026, 11, 11, 0, 2),
    )
    assert (first["close"], first["close_scale"], first["samples"]) == (10_025_000_000, 8, 60)
    assert (second["close"], second["samples"], second["ingested_at"]) == (
        10_025_000_000,
        0,
        PUBLISHED,
    )
    assert mark.problems(t) == []


@pytest.mark.parametrize(
    ("starts", "samples", "message"),
    [
        ([US0 + 1], [60], "whole minute"),
        ([US0, US0], [60, 60], "repeated or out of order"),
        ([US0 + MINUTE, US0], [60, 60], "repeated or out of order"),
        ([US0], [60, 60], "different lengths"),
        ([US0], [70_000], "70000"),  # beyond what a count of samples can be
    ],
)
def test_malformed_minutes_are_refused(starts: list[int], samples: list[int], message: str) -> None:
    with pytest.raises(mark.MarkError, match=message):
        _table(starts, ["1"] * len(starts), samples)


def test_impossible_rows_are_reported_not_refused() -> None:
    t = _table([US0, US0 + MINUTE, US0 + 2 * MINUTE], ["0", "-1.5", "2"], [60, 60, -1])
    assert mark.problems(t) == [
        "mark price not positive: 2 row(s), first AAAUSDT 2026-11-11 00:00:00+00:00",
        "negative sample count: 1 row(s), first AAAUSDT 2026-11-11 00:02:00+00:00",
    ]


def test_days_of_one_contract_join_into_one_table() -> None:
    both = mark.concat([_table([US0], ["1"]), _table([US0 + MINUTE], ["2"])])
    assert both.num_rows == 2
    assert _table([], []).num_rows == 0
