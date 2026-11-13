"""Provenance: everything needed to recompute a result, and its fingerprint.

A ``RunManifest`` records what a run depended on:

| field         | meaning                                                     |
|---------------|-------------------------------------------------------------|
| code          | git commit, and whether the working tree had local changes  |
| environment   | fingerprint of the lock file, Python version and platform   |
| config        | fingerprint of every parameter                              |
| data          | fingerprint of each data partition read, by name            |
| seed          | random seed                                                 |
| clock_limit   | the latest instant the run was allowed to see               |

These fields, and nothing else, determine the result fingerprint. Two runs
with the same result fingerprint must produce byte-identical output; a run
from a working tree with local changes cannot promise that and is marked
not reproducible.
"""

import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from finresearch.core.fingerprint import (
    Fingerprint,
    canonical_json,
    digest_bytes,
    digest_file,
    digest_value,
)
from finresearch.core.time import ensure_utc, format_utc, parse_utc

MANIFEST_VERSION = 1
_COMMIT_HEX_LENGTH = 40


class ProvenanceError(RuntimeError):
    """Provenance could not be established."""


@dataclass(frozen=True, slots=True)
class CodeVersion:
    commit: str
    dirty: bool

    def __post_init__(self) -> None:
        if len(self.commit) != _COMMIT_HEX_LENGTH or not all(
            c in "0123456789abcdef" for c in self.commit
        ):
            raise ProvenanceError(f"not a full git commit id: {self.commit!r}")


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise ProvenanceError("git is not installed")
    result = subprocess.run(  # noqa: S603 - fixed executable, fixed arguments
        [git, "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ProvenanceError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def code_version(repo: Path) -> CodeVersion:
    """The commit checked out in ``repo``; ``dirty`` if any tracked or untracked file differs."""
    commit = _git(repo, "rev-parse", "HEAD").strip()
    dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=normal").strip())
    return CodeVersion(commit, dirty)


def environment_fingerprint(
    lock_file: Path,
    python: str | None = None,
    machine: str | None = None,
    system: str | None = None,
) -> Fingerprint:
    """Lock file contents plus interpreter and platform (results may differ across CPUs)."""
    return digest_value(
        {
            "lock": str(digest_file(lock_file)),
            "python": python or platform.python_version(),
            "implementation": sys.implementation.name,
            "machine": machine or platform.machine(),
            "system": system or platform.system(),
        }
    )


@dataclass(frozen=True, slots=True)
class RunManifest:
    code: CodeVersion
    environment: Fingerprint
    config: Fingerprint
    data: Mapping[str, Fingerprint] = field(default_factory=lambda: dict[str, Fingerprint]())
    seed: int = 0
    clock_limit: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", dict(sorted(self.data.items())))
        if self.seed < 0:
            raise ProvenanceError("seed must be a non-negative integer")
        if self.clock_limit is not None:
            object.__setattr__(self, "clock_limit", ensure_utc(self.clock_limit))

    @property
    def reproducible(self) -> bool:
        return not self.code.dirty

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": MANIFEST_VERSION,
            "code": {"commit": self.code.commit, "dirty": self.code.dirty},
            "environment": str(self.environment),
            "config": str(self.config),
            "data": {name: str(fp) for name, fp in self.data.items()},
            "seed": self.seed,
            "clock_limit": None if self.clock_limit is None else format_utc(self.clock_limit),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RunManifest:
        if d.get("manifest_version") != MANIFEST_VERSION:
            raise ProvenanceError(f"unsupported manifest version {d.get('manifest_version')!r}")
        clock = d.get("clock_limit")
        return cls(
            code=CodeVersion(str(d["code"]["commit"]), bool(d["code"]["dirty"])),
            environment=Fingerprint.parse(str(d["environment"])),
            config=Fingerprint.parse(str(d["config"])),
            data={str(k): Fingerprint.parse(str(v)) for k, v in d.get("data", {}).items()},
            seed=int(d.get("seed", 0)),
            clock_limit=None if clock is None else parse_utc(str(clock)),
        )

    @property
    def fingerprint(self) -> Fingerprint:
        """The result fingerprint: a digest of exactly the fields above."""
        return digest_bytes(canonical_json(self.to_dict()))
