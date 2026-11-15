import io
import zipfile

import pytest

from finresearch.data import sheets
from finresearch.data.sheets import SheetError

_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>'
)
_WB = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="data" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WB_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/></Relationships>'
)


def _cell(ref: str, value: object) -> str:
    if isinstance(value, str):
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
    return f'<c r="{ref}"><v>{value}</v></c>'


def workbook(grid: list[list[object]]) -> bytes:
    """A minimal .xlsx with one sheet, written by hand (no spreadsheet library needed)."""
    rows = "".join(
        f'<row r="{i}">'
        + "".join(_cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(r))
        + "</row>"
        for i, r in enumerate(grid, start=1)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows}</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _WB)
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_cells_come_back_as_stored() -> None:
    grid = sheets.rows(workbook([["DATE", "X65M11"], ["1947:Q1", 306.4], ["1947:Q2", "#N/A"]]))
    assert grid == [["DATE", "X65M11"], ["1947:Q1", 306.4], ["1947:Q2", "#N/A"]]


def test_a_missing_sheet_or_bad_bytes_are_errors() -> None:
    with pytest.raises(SheetError, match="1 sheet"):
        sheets.rows(workbook([["a"]]), sheet=1)
    with pytest.raises(SheetError, match="not a readable workbook"):
        sheets.rows(b"not a workbook")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (306.4, "306.4"),
        (29762.0, "29762"),
        (0.1 + 0.2, "0.30000000000000004"),
        (7, "7"),
        (1e-05, "0.000010"),
    ],
)
def test_number_text_is_the_shortest_decimal_of_the_float(value: float, text: str) -> None:
    assert sheets.number_text(value) == text


def test_number_text_refuses_what_is_not_a_finite_number() -> None:
    with pytest.raises(SheetError):
        sheets.number_text(float("nan"))
    with pytest.raises(SheetError):
        sheets.number_text(True)
