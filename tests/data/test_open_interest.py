import pyarrow as pa
import pytest

from finresearch.core.time import utc
from finresearch.data import open_interest
from finresearch.data.fixed import Fixed, parse

T0 = utc(2026, 11, 11)
US0 = int(T0.timestamp()) * 1_000_000
MINUTE = 60_000_000  # microseconds
PUBLISHED = utc(2026, 11, 12, 1, 8)


def _values(n: int, **replace: Fixed) -> dict[str, Fixed]:
    values = {name: parse(["1"] * n) for name in open_interest.DECIMALS}
    return {**values, **replace}


def _table(labels: list[int], **replace: Fixed) -> open_interest.PitTable:
    return open_interest.readings(
        "binance",
        "AAAUSDT",
        pa.array(labels, pa.int64()),
        5 * MINUTE,
        _values(len(labels), **replace),
        PUBLISHED,
    )


def test_a_reading_is_available_one_period_after_its_label() -> None:
    t = _table(
        [US0, US0 + 5 * MINUTE],
        open_interest=parse(["81655.78", "81686.598"]),
        account_ratio=parse(["1.25", ""], blanks=True),
    )
    first, second = t.table.to_pylist()
    assert (t.name, t.keys) == ("u1.open_interest_5m", ("exchange", "symbol"))
    assert (first["event_time"], first["available_at"]) == (T0, utc(2026, 11, 11, 0, 5))
    assert (second["event_time"], second["available_at"]) == (
        utc(2026, 11, 11, 0, 5),
        utc(2026, 11, 11, 0, 10),
    )
    # Three decimals for the column: 81655.78 is 81,655,780 thousandths.
    assert (first["open_interest"], first["open_interest_scale"]) == (81_655_780, 3)
    assert (second["open_interest"], second["ingested_at"]) == (81_686_598, PUBLISHED)
    assert (first["account_ratio"], second["account_ratio"]) == (125, None)
    assert open_interest.problems(t) == []


@pytest.mark.parametrize(
    ("labels", "replace", "message"),
    [
        ([US0, US0], {}, "repeated or out of order"),
        ([US0 + MINUTE, US0], {}, "repeated or out of order"),
        ([US0], {"open_interest": parse(["1", "2"])}, "different lengths"),
        ([US0], {"open_interest": parse([""], blanks=True)}, "open interest is missing"),
    ],
)
def test_malformed_readings_are_refused(
    labels: list[int], replace: dict[str, Fixed], message: str
) -> None:
    with pytest.raises(open_interest.OpenInterestError, match=message):
        _table(labels, **replace)


def test_the_value_columns_are_fixed() -> None:
    with pytest.raises(open_interest.OpenInterestError, match="need exactly"):
        open_interest.readings(
            "binance",
            "AAAUSDT",
            pa.array([US0], pa.int64()),
            0,
            {"open_interest": parse(["1"])},
            T0,
        )
    with pytest.raises(open_interest.OpenInterestError, match="before its label"):
        open_interest.readings(
            "binance", "AAAUSDT", pa.array([US0], pa.int64()), -1, _values(1), T0
        )


def test_impossible_rows_are_reported_not_refused() -> None:
    t = _table(
        [US0, US0 + 5 * MINUTE],
        open_interest=parse(["-1", "2"]),
        account_ratio=parse(["", "-0.5"], blanks=True),
    )
    assert open_interest.problems(t) == [
        "negative open_interest: 1 row(s), first AAAUSDT 2026-11-11 00:00:00+00:00",
        "negative account_ratio: 1 row(s), first AAAUSDT 2026-11-11 00:05:00+00:00",
    ]


def test_days_of_one_contract_join_into_one_table() -> None:
    both = open_interest.concat([_table([US0]), _table([US0 + 5 * MINUTE])])
    assert both.num_rows == 2
    assert _table([]).num_rows == 0
