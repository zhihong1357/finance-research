# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""CSV text from a data source as an Arrow table.

Reading data files is the data layer's job (tests/test_architecture.py), so
sources hand their CSV text to this module rather than calling a CSV reader
themselves. Columns are named by the caller, read as text unless listed as
integers, and never silently turned into nulls: a decimal stays exact text
until ``data/fixed.py`` converts it.
"""

import io
from collections.abc import Collection, Sequence

import pyarrow as pa
import pyarrow.csv as csv


class CsvError(ValueError):
    """Text that does not read as the CSV the caller described."""


def read(
    text: bytes, columns: Sequence[str], integers: Collection[str], *, header: bool
) -> pa.Table:
    """``text`` as a table with ``columns``; ``header`` says whether to skip a first line."""
    types: dict[str, pa.DataType] = {
        c: pa.int64() if c in integers else pa.string() for c in columns
    }
    try:
        return csv.read_csv(
            io.BytesIO(text),
            read_options=csv.ReadOptions(column_names=list(columns), skip_rows=int(header)),
            convert_options=csv.ConvertOptions(column_types=types, strings_can_be_null=False),
        )
    except pa.ArrowInvalid as exc:
        raise CsvError(str(exc)) from exc
