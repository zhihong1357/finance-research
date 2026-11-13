import tomllib
from pathlib import Path

import pytest

from finresearch.cli import main
from finresearch.registry.models import plan_fingerprint
from tests.registry.conftest import CARD, write_registry


def test_registry_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = write_registry(tmp_path)
    assert main(["registry", "check", "--root", str(root)]) == 0
    assert "registry: ok" in capsys.readouterr().out
    (root / "registry/sources/deribit.book_summary.toml").unlink()
    assert main(["registry", "check", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "problem: registry/hypotheses/H-0001.toml: unknown data source" in out
    assert "1 problem(s)" in out


def test_plan_fingerprint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    card = tmp_path / "H-0001.toml"
    card.write_text(CARD, encoding="utf-8")
    assert main(["registry", "plan-fingerprint", str(card)]) == 0
    assert capsys.readouterr().out.strip() == str(plan_fingerprint(tomllib.loads(CARD)["plan"]))
