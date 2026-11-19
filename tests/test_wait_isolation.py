"""Decision 0022: what may change while Phase 2 waits for acceptance, and why that is safe.

Every scheduled workflow runs the command-line entry (``finresearch.cli``) from
``main``. Decision 0017 keeps the files it runs unchanged until Phase 2 is
accepted. Decision 0022 lets the modules added during the wait be fixed in that
time, on one condition: nothing the workflows run can reach them. These tests
are that condition. When Phase 2 is accepted and the entry is wired to these
modules, decision 0022 lapses and this file goes with it.

- Statically, no other module imports them, not even inside a function body.
- At run time, importing the command-line entry loads none of them.
- Their configuration files are read only by them.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "finresearch"
ADDED_DURING_THE_WAIT = (
    "finresearch.meta",
    "finresearch.meta.bench",
    "finresearch.meta.cheats",
    "finresearch.meta.dry_run",
    "finresearch.meta.noise",
    "finresearch.meta.planted",
    "finresearch.model",
    "finresearch.model.signal",
    "finresearch.signals",
    "finresearch.sim",
    "finresearch.sim.capacity",
    "finresearch.sim.costs",
    "finresearch.sim.engine",
    "finresearch.stats.bootstrap",
    "finresearch.stats.clusters",
    "finresearch.stats.crossval",
    "finresearch.stats.hac",
    "finresearch.stats.maxima",
    "finresearch.stats.montecarlo",
    "finresearch.stats.multiple",
    "finresearch.stats.overfitting",
    "finresearch.stats.power",
    "finresearch.stats.sharpe",
    "finresearch.stats.snooping",
    "finresearch.verdict",
)
THEIR_CONFIG = ("costs", "simulator", "noise", "meta")  # their files in config/


def _module_name(path: Path) -> str:
    parts = path.relative_to(SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _added(name: str) -> bool:
    return any(name == m or name.startswith(f"{m}.") for m in ADDED_DURING_THE_WAIT)


OTHERS = sorted(p for p in SRC.rglob("*.py") if not _added(_module_name(p)))
ADDED = sorted(p for p in SRC.rglob("*.py") if _added(_module_name(p)))


def _imports(source: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.extend([node.module, *(f"{node.module}.{a.name}" for a in node.names)])
    return names


def test_the_list_is_complete() -> None:
    # a new module in one of these packages must be listed (and so is held to the same rules)
    assert {_module_name(p) for p in ADDED} == set(ADDED_DURING_THE_WAIT)


@pytest.mark.parametrize("module", OTHERS, ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_older_module_imports_them(module: Path) -> None:
    source = module.read_text(encoding="utf-8")
    assert [n for n in _imports(source) if _added(n)] == []
    assert not any(f'load_toml("{c}")' in source for c in THEIR_CONFIG)


def test_the_command_line_entry_loads_none_of_them() -> None:
    probe = (
        "import sys, finresearch.cli\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('finresearch'))))\n"
    )
    loaded = subprocess.run(  # noqa: S603 - this interpreter, a fixed probe
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=ROOT
    ).stdout.split()
    assert "finresearch.cli" in loaded
    assert [m for m in loaded if _added(m)] == []


def test_the_workflows_do_not_name_them() -> None:
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for m in ADDED_DURING_THE_WAIT:
            assert m not in text, (workflow.name, m)
        for c in THEIR_CONFIG:
            assert f"config/{c}.toml" not in text, (workflow.name, c)
