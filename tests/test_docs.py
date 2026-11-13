"""Documentation integrity: every relative link in a Markdown file points at a file that exists."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
MARKDOWN = sorted(p for p in ROOT.rglob("*.md") if ".venv" not in p.parts and ".git" not in p.parts)


def test_markdown_files_are_found() -> None:
    assert ROOT / "README.md" in MARKDOWN
    assert ROOT / "docs" / "charter.md" in MARKDOWN


@pytest.mark.parametrize("doc", MARKDOWN, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(doc: Path) -> None:
    broken = [
        target
        for target in LINK.findall(doc.read_text(encoding="utf-8"))
        if "://" not in target
        and not target.startswith("mailto:")
        and not (doc.parent / target).exists()
    ]
    assert broken == []
