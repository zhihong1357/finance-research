"""Structural rules that keep research code behind the point-in-time door.

Outside ``finresearch.data``, code may not read files of data directly, and may
not call the unclocked internals that ``PointInTime`` wraps.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "finresearch"
DATA_PACKAGE = SRC / "data"
FORBIDDEN_MODULES = (
    "pyarrow.parquet",
    "pyarrow.dataset",
    "pyarrow.csv",
    "pyarrow.json",
    "pyarrow.feather",
    "pyarrow.ipc",
    "duckdb",
    "pandas",
    "polars",
)
FORBIDDEN_NAMES = {
    ("finresearch.data.table", "versions_as_of"),
    ("finresearch.data.table", "from_parquet"),
}


def violations(source: str) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    aliases: dict[str, str] = {}  # local name -> module, e.g. "pa" -> "pyarrow"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
                if a.name.startswith(FORBIDDEN_MODULES):
                    found.append(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                full = f"{node.module}.{a.name}"
                if node.module.startswith(FORBIDDEN_MODULES):
                    found.append(node.module)
                elif full.startswith(FORBIDDEN_MODULES) or (node.module, a.name) in FORBIDDEN_NAMES:
                    found.append(full)
    for node in ast.walk(tree):  # attribute access such as pa.parquet.read_table
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            full = f"{aliases[node.value.id]}.{node.attr}"
            if full.startswith(FORBIDDEN_MODULES):
                found.append(full)
    return sorted(set(found))


def test_the_checker_catches_bypasses() -> None:
    assert violations("import pyarrow.parquet as pq") == ["pyarrow.parquet"]
    assert violations("from pyarrow import parquet") == ["pyarrow.parquet"]
    assert violations("import pyarrow as pa\nt = pa.parquet.read_table('x')") == ["pyarrow.parquet"]
    assert violations("from finresearch.data.table import versions_as_of") == [
        "finresearch.data.table.versions_as_of"
    ]
    assert violations("import pandas as pd") == ["pandas"]
    assert violations("import pyarrow as pa\nfrom finresearch.data.pit import PointInTime") == []


@pytest.mark.parametrize(
    "module",
    sorted(p for p in SRC.rglob("*.py") if DATA_PACKAGE not in p.parents),
    ids=lambda p: p.relative_to(SRC).as_posix(),
)
def test_no_module_outside_data_bypasses_the_door(module: Path) -> None:
    assert violations(module.read_text(encoding="utf-8")) == []
