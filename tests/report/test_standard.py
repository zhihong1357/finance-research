import pytest

from finresearch.core.fingerprint import digest_bytes
from finresearch.report.standard import (
    BODY_MARKER,
    SECTIONS,
    Header,
    ReportError,
    body_fingerprint,
    parse,
    render,
    render_body,
)

SECTIONS_TEXT = {i: f"section {i} text" for i in range(1, 13)}
HEADER = Header(
    trial_id="T-000001",
    recorded_at="2026-11-13T00:00:00.000000Z",
    hypothesis="H-0001",
    segment="exploration",
    result_fingerprint=str(digest_bytes(b"result")),
    output_fingerprint=str(digest_bytes(b"output")),
)


def test_twelve_sections_in_protocol_order() -> None:
    assert len(SECTIONS) == 12
    body = render_body("Title", SECTIONS_TEXT)
    assert body.startswith("# Title\n\n## 1. 结论\n\nsection 1 text\n")
    positions = [body.index(f"## {i}. {h}") for i, h in enumerate(SECTIONS, start=1)]
    assert positions == sorted(positions)
    assert body.endswith("## 12. 运行清单\n\nsection 12 text\n")


def test_a_missing_or_empty_section_is_an_incomplete_report() -> None:
    with pytest.raises(ReportError, match=r"missing \[6\]"):
        render_body("T", {i: "x" for i in range(1, 13) if i != 6})
    with pytest.raises(ReportError, match="unexpected"):
        render_body("T", {**SECTIONS_TEXT, 13: "x"})
    with pytest.raises(ReportError, match="section 10"):
        render_body("T", {**SECTIONS_TEXT, 10: "  "})


def test_render_and_parse_round_trip() -> None:
    body = render_body("Title", SECTIONS_TEXT)
    text = render(HEADER, body)
    assert BODY_MARKER in text
    assert parse(text) == (HEADER, body)


def test_the_body_fingerprint_is_the_bytes_of_the_body() -> None:
    body = render_body("Title", SECTIONS_TEXT)
    assert body_fingerprint(body) == digest_bytes(body.encode("utf-8"))
    edited = body.replace("section 5 text", "section 5 text, edited")
    assert body_fingerprint(edited) != body_fingerprint(body)


def test_parse_rejects_damaged_reports() -> None:
    text = render(HEADER, render_body("Title", SECTIONS_TEXT))
    with pytest.raises(ReportError, match="no body marker"):
        parse(text.replace(BODY_MARKER, ""))
    with pytest.raises(ReportError, match="试验编号"):
        parse(text.replace("| 试验编号 |", "| 编号 |"))
