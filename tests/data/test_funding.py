import pyarrow as pa
import pytest

from finresearch.core.time import utc
from finresearch.data import funding
from finresearch.data.fixed import parse

T0 = utc(2026, 10, 1)
US0 = int(T0.timestamp()) * 1_000_000
HOUR = 3_600_000_000  # microseconds
PUBLISHED = utc(2026, 11, 2)


def _table(times: list[int], intervals: list[int] | None = None) -> funding.PitTable:
    rates = parse(["0.00010000", "-0.00000074", "0.00012345"][: len(times)])
    return funding.funding(
        "binance",
        "AAAUSDT",
        pa.array(times, pa.int64()),
        rates,
        pa.array(intervals or [8] * len(times), pa.int64()),
        PUBLISHED,
    )


def test_a_settlement_is_available_when_it_happens() -> None:
    # The second settlement was recorded 3 milliseconds after the hour.
    t = _table([US0, US0 + 8 * HOUR + 3000, US0 + 16 * HOUR], [8, 8, 4])
    rows = t.table.to_pylist()
    assert (t.name, t.keys) == ("u1.funding", ("exchange", "symbol"))
    assert [r["event_time"] for r in rows] == [
        utc(2026, 10, 1),
        utc(2026, 10, 1, 8, 0, 0, 3000),
        utc(2026, 10, 1, 16),
    ]
    assert [r["available_at"] for r in rows] == [r["event_time"] for r in rows]
    assert {r["ingested_at"] for r in rows} == {PUBLISHED}
    # One basis point, then -0.74 millionths, at eight decimal places.
    assert [(r["funding_rate"], r["funding_rate_scale"]) for r in rows] == [
        (10000, 8),
        (-74, 8),
        (12345, 8),
    ]
    assert [r["interval_hours"] for r in rows] == [8, 8, 4]


@pytest.mark.parametrize(
    ("times", "intervals", "message"),
    [
        ([US0, US0], [8, 8], "repeated or out of order"),
        ([US0 + HOUR, US0], [8, 8], "repeated or out of order"),
        ([US0], [0], "not positive"),
        ([US0], [8, 8], "different lengths"),
        ([US0], [1000], "too long"),
    ],
)
def test_malformed_settlements_are_refused(
    times: list[int], intervals: list[int], message: str
) -> None:
    with pytest.raises(funding.FundingError, match=message):
        _table(times, intervals)


def test_months_of_one_contract_join_into_one_table() -> None:
    both = funding.concat([_table([US0]), _table([US0 + 8 * HOUR])])
    assert both.num_rows == 2
    assert _table([]).num_rows == 0
