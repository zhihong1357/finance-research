"""Structural rules that keep research code behind the point-in-time door.

Outside ``finresearch.data``, code may not read files of data directly, and may
not call the unclocked internals that ``PointInTime`` wraps.

Studies (``finresearch.studies``) and signals (``finresearch.signals``) are held
to more: one gets a door and its configuration, nothing else. It may not import
the rest of the data layer (raw files, tables), the data sources (a synthetic
source would hand it the truth), the run framework or the registry, and it may
not touch files, processes or the network at all.

Nor may it build a door of its own: the door it is handed is the only one. A
door built from the handed door's tables and a new clock would read past the
decision time and leave no trace in the manifest. So a study or signal may not
construct ``PointInTime`` or ``Clock``, import the clock or vault machinery,
read private attributes (``_name``, ``__dict__``), or reach them indirectly
(``getattr``, ``vars``, ``type``, ``importlib``, ``inspect``). This is a static
check, so it guards against mistakes and shortcuts, not against code written to
evade it; review still reads every study.
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
    "python_calamine",
)
STUDIES_PACKAGE = SRC / "studies"
SIGNALS_PACKAGE = SRC / "signals"  # held to the same rules (Phase 4.1)
STUDY_FORBIDDEN = (
    "finresearch.data",
    "finresearch.sources",
    "finresearch.runner",
    "finresearch.registry",
    "finresearch.cli",
    "pathlib",
    "os",
    "io",
    "shutil",
    "subprocess",
    "socket",
    "urllib",
    "http",
    "pickle",
    "importlib",
    "inspect",
    "sys",
    "builtins",
    "gc",
    "ctypes",
)
STUDY_ALLOWED = ("finresearch.data.pit",)
# what a study or signal may take from the door's module: types and the date helpers
DOOR_NAMES = {
    "PointInTime",
    "Snapshot",
    "start_of_day",
    "end_of_day",
    "AccessRefusedError",
    "LookaheadError",
    "VaultSealedError",
    "NotRegisteredError",
}
DOOR_BUILDERS = {"PointInTime", "Clock"}  # a door is handed over, never built
STUDY_FORBIDDEN_CALLS = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "vars",
    "globals",
    "locals",
    "type",
    "object",
}
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


def _is(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(f"{p}.") for p in prefixes)


def _called(node: ast.Call) -> str | None:
    """The name a call calls: ``f(...)`` and ``m.f(...)`` both give ``f``."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def study_violations(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            if _is(node.module, STUDY_ALLOWED):
                modules = []
                found.extend(
                    f"{node.module}.{a.name}" for a in node.names if a.name not in DOOR_NAMES
                )
        elif isinstance(node, ast.Call):
            name = _called(node)
            builtin = isinstance(node.func, ast.Name) and name in STUDY_FORBIDDEN_CALLS
            if builtin or name in DOOR_BUILDERS:
                found.append(f"{name}()")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            found.append(f".{node.attr}")
        found.extend(m for m in modules if _is(m, STUDY_FORBIDDEN) and not _is(m, STUDY_ALLOWED))
    return sorted(set(found))


def test_the_study_checker_catches_bypasses() -> None:
    assert study_violations("from finresearch.data.pit import PointInTime") == []
    assert study_violations("from finresearch.stats.correlation import pearson") == []
    assert study_violations("from finresearch.data.table import versions_as_of") == [
        "finresearch.data.table",
        "finresearch.data.table.versions_as_of",
    ]
    assert study_violations("from finresearch.data import catalog") == [
        "finresearch.data",
        "finresearch.data.catalog",
    ]
    assert study_violations("from finresearch.sources.synthetic import fetch") == [
        "finresearch.sources.synthetic",
        "finresearch.sources.synthetic.fetch",
    ]
    assert study_violations("import pathlib") == ["pathlib"]
    assert study_violations("data = open('x').read()") == ["open()"]
    assert study_violations("import osmosis") == []


def test_the_study_checker_catches_a_door_built_by_hand() -> None:
    # the bypass the independent review found: the handed door's tables behind a fresh clock
    bypass = (
        "from finresearch.data.pit import Clock, PointInTime\n"
        "def compute(door, at):\n"
        "    return PointInTime(Clock(None, None), door._tables).latest('cpi')\n"
    )
    assert study_violations(bypass) == [
        "._tables",
        "Clock()",
        "PointInTime()",
        "finresearch.data.pit.Clock",
    ]
    assert study_violations("import finresearch.data.pit as p\nd = p.PointInTime(c, t)") == [
        "PointInTime()"
    ]
    assert study_violations("d = type(door)(c, t)") == ["type()"]
    assert study_violations("t = getattr(door, 'tab' + 'les')") == ["getattr()"]
    assert study_violations("t = vars(door)") == ["vars()"]
    assert study_violations("t = door.__dict__") == [".__dict__"]
    assert study_violations("import importlib") == ["importlib"]
    assert study_violations("from finresearch.data.pit import clock_for") == [
        "finresearch.data.pit.clock_for"
    ]
    # what studies do use stays allowed
    allowed = (
        "from finresearch.data.pit import PointInTime, Snapshot, start_of_day\n"
        "def compute(door: PointInTime, at):\n"
        "    return door.view(at).latest('cpi').table.num_rows\n"
    )
    assert study_violations(allowed) == []


@pytest.mark.parametrize(
    "module",
    sorted(STUDIES_PACKAGE.rglob("*.py")),
    ids=lambda p: p.relative_to(SRC).as_posix(),
)
def test_studies_see_only_the_door(module: Path) -> None:
    assert study_violations(module.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "module",
    sorted(SIGNALS_PACKAGE.rglob("*.py")),
    ids=lambda p: p.relative_to(SRC).as_posix(),
)
def test_signals_see_only_the_door(module: Path) -> None:
    assert study_violations(module.read_text(encoding="utf-8")) == []
