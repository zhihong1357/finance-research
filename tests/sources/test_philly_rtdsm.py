from datetime import date
from decimal import Decimal

import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.data import vintages
from finresearch.data.table import versions_as_of
from finresearch.sources.base import SourceError
from finresearch.sources.philly_rtdsm import ADAPTER, PAGE, SITE, known_by, parse, parse_rows
from tests.data.test_sheets import workbook
from tests.sources.conftest import FakeServer

FETCHED = utc(2026, 11, 15, 6, 41)
# Made-up real output, in the data set's layout: periods down, vintages across.
GRID: list[list[object]] = [
    ["DATE", "ROUTPUT26M8", "ROUTPUT26M9", "ROUTPUT26M11"],
    ["2026:Q1", 100.0, 100.0, 100.5],
    ["2026:Q2", "#N/A", 101.2, 101.2],
    ["2026:Q3", "#N/A", "#N/A", 102.0],
]


@pytest.mark.parametrize(
    ("header", "series", "known"),
    [
        ("ROUTPUT65M11", "ROUTPUT", utc(1965, 12, 1)),
        ("EMPLOY64M12", "EMPLOY", utc(1965, 1, 1)),  # 64 is 1964 when fetched in 2026
        ("RUC65Q4", "RUC", utc(1965, 12, 1)),  # the fourth quarter's middle month is November
        ("RUC26Q1", "RUC", utc(2026, 3, 1)),
        ("P26M11", "P", FETCHED),  # not over yet when fetched: public by the fetch
    ],
)
def test_a_vintage_is_known_by_the_end_of_its_month(
    header: str, series: str, known: object
) -> None:
    assert known_by(header, FETCHED) == (series, known)


def test_each_query_sees_the_vintage_public_then() -> None:
    t = parse(workbook(GRID), FETCHED)
    assert t.name == "macro.rtdsm"

    def at(when: object) -> dict[date, Decimal]:
        rows = versions_as_of(t, when).to_pylist()  # type: ignore[arg-type]
        return {r["event_time"].date(): Decimal(r["value"]).scaleb(-r["value_scale"]) for r in rows}

    assert at(utc(2026, 9, 1)) == {date(2026, 1, 1): Decimal("100.0")}
    assert at(utc(2026, 10, 1)) == {
        date(2026, 1, 1): Decimal("100.0"),
        date(2026, 4, 1): Decimal("101.2"),
    }
    assert at(FETCHED) == {
        date(2026, 1, 1): Decimal("100.5"),
        date(2026, 4, 1): Decimal("101.2"),
        date(2026, 7, 1): Decimal("102.0"),
    }
    # Repeats inside the workbook are dropped: Q2's 101.2 appears once, from September.
    assert vintages.revisions(t, "ROUTPUT", date(2026, 4, 1)) == [
        (utc(2026, 10, 1), Decimal("101.2"))
    ]
    # Q1 twice (100.0, then 100.5 in November), Q2 and Q3 once each.
    assert t.num_rows == 4


def test_monthly_observations() -> None:
    t = parse_rows([["DATE", "PCPI26M10"], ["2026:09", 330.123]], FETCHED)
    assert t.table.column("frequency").to_pylist() == ["M"]
    assert vintages.revisions(t, "PCPI", date(2026, 9, 1)) == [
        (utc(2026, 11, 1), Decimal("330.123"))
    ]


@pytest.mark.parametrize(
    ("grid", "message"),
    [
        ([["YEAR", "X26M8"], ["2026:Q1", 1.0]], "not DATE"),
        ([["DATE", "X26M8"], ["2026-Q1", 1.0]], "not an observation period"),
        ([["DATE", "X26M8"], ["2026:Q1", "high"]], "not a number"),
        ([["DATE", "hello"], ["2026:Q1", 1.0]], "not a vintage column"),
        ([["DATE", "X26M13"], ["2026:Q1", 1.0]], "names no month"),
        ([["DATE", "X26M8", "Y26M8"], ["2026:Q1", 1.0, 2.0]], "more than one variable"),
        ([["DATE", "X26M8"], ["2026:Q1", 1.0], ["2026:01", 1.0]], "monthly and quarterly"),
        # A vintage dated before the period ended would be a look into the future.
        ([["DATE", "X26M2"], ["2026:Q1", 1.0]], "before it ends"),
    ],
)
def test_unexpected_content_is_refused(grid: list[list[object]], message: str) -> None:
    with pytest.raises(SourceError, match=message):
        parse(workbook(grid), FETCHED)


def test_fetch_finds_each_workbook_through_its_page(server: FakeServer) -> None:
    link = "/-/media/FRBP/x/data-files/xlsx/routputMvQd.xlsx?sc_lang=en&amp;hash=AB12"

    def handler(r: httpx2.Request) -> httpx2.Response:
        if str(r.url) == f"{PAGE}/routput":
            return httpx2.Response(
                200, content=f'<a href="{link}">x</a><a href="{link}">y</a>'.encode()
            )
        return httpx2.Response(200, content=b"workbook")

    server.handler = handler
    (response,) = ADAPTER.fetch({"files": {"routput": "routputMvQd.xlsx"}}, FETCHED)
    assert response.name == "routputMvQd"
    assert (
        response.url
        == SITE + "/-/media/FRBP/x/data-files/xlsx/routputMvQd.xlsx?sc_lang=en&hash=AB12"
    )
    assert response.content == b"workbook"
    server.handler = lambda _r: httpx2.Response(200, content=b"<p>no links</p>")
    with pytest.raises(SourceError, match="0 links"):
        ADAPTER.fetch({"files": {"routput": "routputMvQd.xlsx"}}, FETCHED)


def test_a_correction_to_an_old_vintage_wins_when_merged_latest() -> None:
    before = parse(workbook(GRID), FETCHED)
    fixed_grid = [row[:] for row in GRID]
    fixed_grid[1][1] = 99.9  # the curator corrects the August vintage's Q1 value
    after = parse(workbook(fixed_grid), utc(2026, 11, 22, 6, 41))
    with pytest.raises(vintages.VintageError, match="disagree"):
        vintages.merge("macro.rtdsm", [before, after])
    t = vintages.merge("macro.rtdsm", [before, after], vintages.Conflicts.LATEST)
    assert vintages.revisions(t, "ROUTPUT", date(2026, 1, 1))[0] == (
        utc(2026, 9, 1),
        Decimal("99.9"),
    )
