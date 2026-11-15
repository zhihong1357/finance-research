from datetime import date, datetime
from decimal import Decimal

import pytest

from finresearch.core.time import utc
from finresearch.data import vintages
from finresearch.data.fixed import parse
from finresearch.data.pit import Clock, PointInTime
from finresearch.data.table import PitTable, versions_as_of
from finresearch.data.vintages import Frequency, VintageError

NAME = "test.vintages"
FETCHED = utc(2026, 11, 15)
MAY = utc(2026, 5, 15)
JUNE = utc(2026, 6, 15)


def _output(texts: list[str], known: list[datetime]) -> PitTable:
    # Real output for the first quarter of 2026: first estimate in May, revised in June.
    return vintages.versions(
        NAME,
        "ROUTPUT",
        Frequency.QUARTER,
        [date(2026, 1, 1)] * len(texts),
        parse(texts),
        known,
        FETCHED,
    )


def test_a_query_sees_the_version_public_at_its_instant() -> None:
    t = _output(["24010.5", "24031.2"], [MAY, JUNE])
    before = versions_as_of(t, utc(2026, 6, 14)).to_pylist()
    after = versions_as_of(t, JUNE).to_pylist()
    assert [(r["value"], r["value_scale"]) for r in before] == [(240105, 1)]
    assert [(r["value"], r["value_scale"]) for r in after] == [(240312, 1)]
    assert versions_as_of(t, utc(2026, 5, 14)).num_rows == 0
    assert vintages.revisions(t, "ROUTPUT", date(2026, 1, 1)) == [
        (MAY, Decimal("24010.5")),
        (JUNE, Decimal("24031.2")),
    ]


def test_the_door_answers_with_the_version_of_the_moment() -> None:
    pit = PointInTime(Clock(limit=FETCHED), [_output(["24010.5", "24031.2"], [MAY, JUNE])])
    assert pit.as_of(NAME, utc(2026, 6, 1)).column("value") == [240105]
    assert pit.as_of(NAME, utc(2026, 7, 1)).column("value") == [240312]


@pytest.mark.parametrize(
    ("start", "frequency", "end"),
    [
        (date(2026, 2, 28), Frequency.DAY, date(2026, 3, 1)),
        (date(2026, 12, 1), Frequency.MONTH, date(2027, 1, 1)),
        (date(2026, 10, 1), Frequency.QUARTER, date(2027, 1, 1)),
        (date(2026, 4, 1), Frequency.QUARTER, date(2026, 7, 1)),
    ],
)
def test_period_ends(start: date, frequency: Frequency, end: date) -> None:
    assert vintages.period_end(start, frequency) == end


@pytest.mark.parametrize(
    ("start", "frequency", "message"),
    [
        (date(2026, 3, 2), Frequency.MONTH, "start on the 1st"),
        (date(2026, 2, 1), Frequency.QUARTER, "January, April, July or October"),
    ],
)
def test_a_period_must_start_on_its_first_day(
    start: date, frequency: Frequency, message: str
) -> None:
    with pytest.raises(VintageError, match=message):
        vintages.period_end(start, frequency)


def test_a_value_cannot_be_public_before_its_period_ends() -> None:
    # The first quarter ends on 1 April; a version dated 31 March would be a look into the future.
    with pytest.raises(VintageError, match="before it ends"):
        _output(["24010.5"], [utc(2026, 3, 31, 23, 59)])
    # A day's rate published at 14:30 New York time the next day (19:30 UTC) is fine.
    t = vintages.versions(
        NAME,
        "SOFR",
        Frequency.DAY,
        [date(2026, 11, 12)],
        parse(["3.91"]),
        [utc(2026, 11, 13, 19, 30)],
        FETCHED,
    )
    assert t.num_rows == 1


@pytest.mark.parametrize(
    ("texts", "known", "message"),
    [
        (["1.0", "2.0"], [MAY], "different lengths"),
        (["1.0", ""], [MAY, JUNE], "missing"),
    ],
)
def test_bad_columns_are_refused(texts: list[str], known: list[datetime], message: str) -> None:
    with pytest.raises(VintageError, match=message):
        vintages.versions(
            NAME,
            "ROUTPUT",
            Frequency.QUARTER,
            [date(2026, 1, 1)] * 2,
            parse(texts, blanks=True),
            known,
            FETCHED,
        )


def test_compact_drops_repeats_and_keeps_what_queries_see() -> None:
    # Five monthly vintages of one quarter: 1.5, 1.50 (same value, more decimals), 1.6, 1.6, 1.5.
    known = [utc(2026, m, 15) for m in (5, 6, 7, 8, 9)]
    parts = [
        vintages.versions(
            NAME, "ROUTPUT", Frequency.QUARTER, [date(2026, 1, 1)], parse([text]), [k], FETCHED
        )
        for text, k in zip(["1.5", "1.50", "1.6", "1.6", "1.5"], known, strict=True)
    ]
    full = vintages.concat(NAME, parts)
    small = vintages.compact(full)
    assert small.table.column("available_at").to_pylist() == [known[0], known[2], known[4]]
    for at in [*known, utc(2026, 12, 1)]:
        a = versions_as_of(full, at).to_pylist()
        b = versions_as_of(small, at).to_pylist()
        assert [Decimal(r["value"]).scaleb(-r["value_scale"]) for r in a] == [
            Decimal(r["value"]).scaleb(-r["value_scale"]) for r in b
        ]


def test_compact_keeps_different_observations_and_series_apart() -> None:
    q1 = vintages.versions(
        NAME,
        "A",
        Frequency.QUARTER,
        [date(2026, 1, 1), date(2026, 4, 1)],
        parse(["7", "7"]),
        [JUNE, utc(2026, 7, 15)],
        FETCHED,
    )
    other = vintages.versions(
        NAME, "B", Frequency.QUARTER, [date(2026, 1, 1)], parse(["7"]), [JUNE], FETCHED
    )
    assert vintages.compact(vintages.concat(NAME, [q1, other])).num_rows == 3


def test_concat_refuses_a_series_with_two_frequencies_and_other_tables() -> None:
    monthly = vintages.versions(
        NAME, "A", Frequency.MONTH, [date(2026, 1, 1)], parse(["1"]), [JUNE], FETCHED
    )
    quarterly = vintages.versions(
        NAME, "A", Frequency.QUARTER, [date(2026, 1, 1)], parse(["1"]), [utc(2026, 7, 1)], FETCHED
    )
    with pytest.raises(VintageError, match="more than one frequency"):
        vintages.concat(NAME, [monthly, quarterly])
    with pytest.raises(VintageError, match="cannot put"):
        vintages.concat("other.name", [monthly])
    assert vintages.concat(NAME, []).num_rows == 0
