import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.sources.base import SourceError
from finresearch.sources.releases import BEA_ADAPTER, BEA_ARCHIVE, BLS_ADAPTER, BLS_ARCHIVE
from tests.sources.conftest import FakeServer

FETCHED = utc(2026, 11, 16, 6, 41)


def _bea_page(rows: list[tuple[str, str, str]]) -> bytes:
    """Rows of the archive table as BEA writes them (made-up releases)."""
    body = "".join(
        f'<tr class="release-row"><td><a href="{href}" hreflang="en">{title}</a></td>'
        f'<td><time datetime="{when}">x</time></td></tr>'
        for href, title, when in rows
    )
    return f"<table><tbody>{body}</tbody></table>".encode()


def test_bea_rows_carry_their_exact_publication_time() -> None:
    page = _bea_page(
        [
            (
                "/news/2026/gdp-advance",
                "GDP (Advance Estimate), 2nd Quarter 2026",
                "2026-07-30T08:31:00-04:00",
            ),
            ("/news/2026/gdp-second", "GDP &amp; Profits", "2026-08-26T08:30:00-04:00"),
        ]
    )
    t = BEA_ADAPTER.parse_record(
        page, FETCHED, f"{BEA_ARCHIVE}?field_related_product_target_id=451&page=0"
    )
    rows = t.table.to_pylist()
    assert (t.name, t.keys) == ("macro.releases", ("release",))
    assert [r["event_time"] for r in rows] == [utc(2026, 7, 30, 12, 31), utc(2026, 8, 26, 12, 30)]
    assert [r["available_at"] for r in rows] == [r["event_time"] for r in rows]
    assert {r["release"] for r in rows} == {"BEA.GDP"}
    assert rows[1]["title"] == "GDP & Profits"
    assert rows[0]["page"] == "https://www.bea.gov/news/2026/gdp-advance"
    assert {r["timing"] for r in rows} == {"archive"}


def test_bea_a_day_only_release_is_known_by_the_end_of_that_day_not_at_midnight() -> None:
    page = _bea_page([("/news/2005/gdp", "GDP", "2005-01-28T00:00:00-05:00")])
    (row,) = BEA_ADAPTER.parse_record(
        page, FETCHED, f"{BEA_ARCHIVE}?field_related_product_target_id=451&page=8"
    ).table.to_pylist()
    assert (row["event_time"], row["available_at"], row["timing"]) == (
        utc(2005, 1, 28, 5),
        utc(2005, 1, 29, 5),
        "date_only",
    )


def test_bea_a_day_only_release_fetched_that_same_day_is_known_by_the_fetch() -> None:
    page = _bea_page([("/news/2026/x", "x", "2026-11-16T00:00:00-05:00")])
    (row,) = BEA_ADAPTER.parse_record(
        page, FETCHED, f"{BEA_ARCHIVE}?field_related_product_target_id=451&page=0"
    ).table.to_pylist()
    assert row["available_at"] == FETCHED


def test_bea_refuses_an_unknown_product_and_a_future_release() -> None:
    page = _bea_page([("/news/2026/x", "x", "2026-12-01T08:30:00-05:00")])
    with pytest.raises(SourceError, match="unknown product"):
        BEA_ADAPTER.parse_record(
            page, FETCHED, f"{BEA_ARCHIVE}?field_related_product_target_id=1&page=0"
        )
    with pytest.raises(SourceError, match="after the fetch"):
        BEA_ADAPTER.parse_record(
            page, FETCHED, f"{BEA_ARCHIVE}?field_related_product_target_id=451&page=0"
        )


def test_bea_fetch_stops_at_the_first_empty_page(server: FakeServer) -> None:
    full = _bea_page([("/news/2026/a", "a", "2026-07-30T08:30:00-04:00")])
    server.handler = lambda r: httpx2.Response(
        200, content=full if r.url.params["page"] == "0" else b"<p></p>"
    )
    responses = BEA_ADAPTER.fetch({"products": {"GDP": 451}, "max_pages": 30}, FETCHED)
    assert [r.name for r in responses] == ["GDP-0", "GDP-1"]


def _bls_page(names: list[str], folder: str = "archives") -> bytes:
    return "".join(f'<li><a href="/news.release/{folder}/{n}">x</a></li>' for n in names).encode()


def test_bls_dates_come_from_the_file_names_at_half_past_eight_in_new_york() -> None:
    page = _bls_page(
        ["cpi_10142026.htm", "cpi_10142026.pdf", "cpi_11132026.htm", "empsit_11062026.htm"]
    )
    t = BLS_ADAPTER.parse_record(page, FETCHED, f"{BLS_ARCHIVE}/cpi.htm")
    rows = t.table.to_pylist()
    # One row per date (the PDF is the same release); the other program's file is ignored.
    assert [r["event_time"] for r in rows] == [utc(2026, 10, 14, 12, 30), utc(2026, 11, 13, 13, 30)]
    assert {r["release"] for r in rows} == {"BLS.CPI"}
    assert {r["timing"] for r in rows} == {"schedule"}
    assert rows[0]["page"] == "https://www.bls.gov/news.release/archives/cpi_10142026.htm"


def test_bls_reads_old_releases_from_the_history_folder() -> None:
    page = _bls_page(["cpi_12072001.txt"], folder="history") + _bls_page(["cpi_10142026.htm"])
    rows = BLS_ADAPTER.parse_record(page, FETCHED, f"{BLS_ARCHIVE}/cpi.htm").table.to_pylist()
    assert [r["event_time"] for r in rows] == [utc(2001, 12, 7, 13, 30), utc(2026, 10, 14, 12, 30)]
    assert rows[0]["page"] == "https://www.bls.gov/news.release/history/cpi_12072001.txt"


def test_bls_ignores_placeholder_links_inside_html_comments() -> None:
    page = (
        b"<!-- "
        + _bls_page(["empsit_12042026.htm", "empsit_2027.htm"])
        + b" -->"
        + _bls_page(["empsit_11062026.htm"])
    )
    rows = BLS_ADAPTER.parse_record(page, FETCHED, f"{BLS_ARCHIVE}/empsit.htm").table.to_pylist()
    assert [r["event_time"] for r in rows] == [utc(2026, 11, 6, 13, 30)]


def test_bls_refuses_a_file_name_that_is_not_a_date() -> None:
    with pytest.raises(SourceError, match="names no date"):
        BLS_ADAPTER.parse_record(_bls_page(["cpi_13402026.htm"]), FETCHED, f"{BLS_ARCHIVE}/cpi.htm")


def test_bls_fetch_needs_a_contact_and_sends_it(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"programs": ["cpi"], "agent": "finresearch/0.1"}
    monkeypatch.delenv("BLS_CONTACT", raising=False)
    with pytest.raises(SourceError, match="BLS_CONTACT is not set"):
        BLS_ADAPTER.fetch(config, FETCHED)
    monkeypatch.setenv("BLS_CONTACT", "someone@example.invalid")
    server.handler = lambda _r: httpx2.Response(200, content=b"<p></p>")
    (response,) = BLS_ADAPTER.fetch(config, FETCHED)
    assert response.url == f"{BLS_ARCHIVE}/cpi.htm"
    assert (
        server.requests[0].headers["User-Agent"]
        == "finresearch/0.1 (contact: someone@example.invalid)"
    )
