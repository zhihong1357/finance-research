"""Spreadsheet files from a data source as rows of cells (Phase 2.6.4).

Reading data files is the data layer's job (tests/test_architecture.py), so
sources hand their Excel bytes to this module. ``python-calamine`` reads both
``.xlsx`` and the older ``.xls``; openpyxl cannot read the Philadelphia Fed's
workbooks (their metadata trips it, docs/architecture.md 7.3).

A cell comes back as Excel stored it: a float for a number, a string for text.
Excel keeps numbers as 64-bit floats, so ``number_text`` turns a float into the
shortest decimal that reads back as the same float, which is the decimal the
publisher typed (up to 15 significant digits, data/fixed.py).
"""

import io
import math

from python_calamine import CalamineWorkbook

type Cell = float | int | str | bool | None


class SheetError(ValueError):
    """Bytes that are not a workbook, or a sheet that is not there."""


def rows(content: bytes, sheet: int = 0) -> list[list[Cell]]:
    """The cells of one sheet (by position), row by row."""
    try:
        wb = CalamineWorkbook.from_filelike(io.BytesIO(content))
        names = wb.sheet_names
        if sheet >= len(names):
            raise SheetError(f"the workbook has {len(names)} sheet(s), not {sheet + 1}")
        data = wb.get_sheet_by_index(sheet).to_python()
    except SheetError:
        raise
    except Exception as exc:  # the reader raises its own exception types
        raise SheetError(f"not a readable workbook: {type(exc).__name__}") from exc
    return [[c if isinstance(c, float | int | str | bool) else None for c in r] for r in data]


def number_text(value: float | int) -> str:
    """A number cell as decimal text: ``306.4`` for the float nearest 306.4."""
    if isinstance(value, bool) or not math.isfinite(value):
        raise SheetError(f"not a finite number: {value!r}")
    if isinstance(value, int):
        return str(value)
    text = repr(value)
    if "e" in text or "E" in text:
        return format(value, "f")
    return text[:-2] if text.endswith(".0") else text
