"""Compare the registry with an earlier commit: history may only grow.

The hash chain inside each log catches an edited line, but not a log rewritten
from scratch with a fresh, internally consistent chain. Comparing with git
history catches that too:

* each append-only log at ``base`` must be a byte-for-byte prefix of the log now;
* each hypothesis card at ``base`` keeps its history entries unchanged (new
  entries may follow), and a card registered at ``base`` keeps its plan exactly.
"""

import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from finresearch.core.provenance import ProvenanceError
from finresearch.registry.validate import APPEND_ONLY, HYPOTHESES

_NO_COMMIT = "0" * 40  # what GitHub reports as "before" for the first push of a branch


def _git_bytes(repo: Path, *args: str) -> bytes | None:
    git = shutil.which("git")
    if git is None:
        raise ProvenanceError("git is not installed")
    result = subprocess.run(  # noqa: S603 - fixed executable, arguments built here
        [git, "-C", str(repo), *args], capture_output=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def _at(repo: Path, rev: str, rel: Path) -> bytes | None:
    return _git_bytes(repo, "show", f"{rev}:{rel.as_posix()}")


def _cards_at(repo: Path, rev: str) -> list[Path]:
    out = _git_bytes(repo, "ls-tree", "--name-only", f"{rev}:{HYPOTHESES.as_posix()}")
    if not out:
        return []
    return [HYPOTHESES / name for name in out.decode().split() if name.endswith(".toml")]


def check_append_only(repo: Path, base: str) -> list[str]:
    if base == _NO_COMMIT:
        return []
    if _git_bytes(repo, "rev-parse", "--verify", f"{base}^{{commit}}") is None:
        return [f"base revision {base} not found (is the checkout deep enough?)"]
    problems: list[str] = []
    for rel in APPEND_ONLY:
        old = _at(repo, base, rel)
        if old is None:
            continue
        path = repo / rel
        if not path.exists():
            problems.append(f"{rel}: deleted, but it is append-only")
        elif not path.read_bytes().startswith(old):
            problems.append(
                f"{rel}: existing lines were changed or removed (only appending is allowed)"
            )
    for rel in _cards_at(repo, base):
        problems.extend(_check_card(repo, base, rel))
    return problems


def _check_card(repo: Path, base: str, rel: Path) -> list[str]:
    old_text = _at(repo, base, rel)
    path = repo / rel
    if old_text is None:
        return []
    if not path.exists():
        return [f"{rel}: hypothesis cards are never deleted (rejected ones stay as a record)"]
    try:
        old: dict[str, Any] = tomllib.loads(old_text.decode())
        new: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return []  # reported by the schema check
    problems: list[str] = []
    old_history: list[Any] = old.get("history", [])
    if new.get("history", [])[: len(old_history)] != old_history:
        problems.append(f"{rel}: earlier history entries were changed (only appending is allowed)")
    if any(e.get("status") == "registered" for e in old_history) and new.get("plan") != old.get(
        "plan"
    ):
        problems.append(f"{rel}: the plan was changed after registration")
    return problems
