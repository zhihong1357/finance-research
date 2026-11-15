"""Offsite backup (Phase 2.9, decision 0014): raw data that cannot be fetched again, twice.

Once a day, for every source in ``config/backup.toml``, each finished day of
raw files (``raw/<source>/<YYYY>/<MM>/<DD>/``) that is not in the backup yet is
packed into one tar file, ``<source>__<YYYY-MM-DD>.tar``, and uploaded to the
offsite store (Google Drive, ``data/gdrive.py``). The pack holds the files as
stored, plus ``MANIFEST.json`` with each file's SHA-256; it is built the same
way every time (sorted names, fixed times and owners), so the same day always
gives the same bytes. Today is never packed: it is not finished.

``drill`` is the restore check: it downloads a few packs at random, unpacks
them, and compares every file byte for byte with the remote store.

Each run leaves a small log in the remote store (``backup/log/...``), which the
monitor reads to open an issue when backups stop (``monitor.check_backup``).
Logs carry counts only (decision 0008).
"""

import gzip
import hashlib
import io
import json
import random
import re
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc
from finresearch.data.gdrive import DriveFile
from finresearch.data.store import Limits, Remote, pull, push, with_retries

LOG = "backup/log"
MANIFEST = "MANIFEST.json"
_DAY = re.compile(r"^(raw/[^/]+/(\d{4})/(\d{2})/(\d{2}))/[^/]+$")


class BackupError(RuntimeError):
    """A pack that cannot be made, or a backup that does not match the remote store."""


class Offsite(Protocol):
    """The offsite store: one folder of files (``data/gdrive.Drive`` in production)."""

    def folder(self, name: str) -> str: ...

    def list(self, folder_id: str) -> dict[str, DriveFile]: ...

    def upload(self, folder_id: str, name: str, content: bytes) -> DriveFile: ...

    def download(self, file_id: str) -> bytes: ...


def backup_config() -> dict[str, Any]:
    return load_toml("backup")


def days(remote: Remote, source: str, limits: Limits) -> dict[str, str]:
    """Every day folder of ``source`` in the remote store: pack name -> folder."""
    listing = with_retries(lambda: remote.list(f"raw/{source}"), limits)
    out: dict[str, str] = {}
    for path in listing:
        m = _DAY.match(path)
        if m:
            folder, y, mo, d = m.groups()
            out[f"{source}__{y}-{mo}-{d}.tar"] = folder
    return out


def pack(local: Path, folder: str) -> bytes:
    """The files under ``local/folder`` and their manifest, as one reproducible tar."""
    root = local / folder
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        raise BackupError(f"{folder}: nothing to pack")
    manifest = {
        p.relative_to(local).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        entries = [(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True).encode())]
        entries += [(p.relative_to(local).as_posix(), p.read_bytes()) for p in files]
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(content: bytes) -> dict[str, bytes]:
    """The files of a pack (path -> bytes), each checked against the pack's manifest."""
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as tar:
        files: dict[str, bytes] = {}
        for member in tar.getmembers():
            fh = tar.extractfile(member)
            if fh is None:
                raise BackupError(f"{member.name}: not a regular file")
            files[member.name] = fh.read()
    manifest: dict[str, str] = json.loads(files.pop(MANIFEST, b"{}"))
    if set(manifest) != set(files):
        raise BackupError("the pack's files do not match its manifest")
    for name, data in files.items():
        if hashlib.sha256(data).hexdigest() != manifest[name]:
            raise BackupError(f"{name}: does not match the pack's manifest")
    return files


@dataclass(frozen=True)
class Report:
    uploaded: int
    already: int
    pending: int  # finished days left for a later run (max_days_per_run)


def run(
    cfg: Mapping[str, Any],
    remote: Remote,
    offsite: Offsite,
    limits: Limits,
    work: Path,
    now: datetime,
) -> Report:
    """Upload every finished day not backed up yet (at most ``max_days_per_run``); log the run."""
    now = ensure_utc(now)
    today = f"{now:%Y-%m-%d}"
    folder_id = offsite.folder(str(cfg["folder"]))
    have = offsite.list(folder_id)
    todo: list[tuple[str, str]] = []
    already = 0
    for source in cfg["sources"]:
        for name, folder in sorted(days(remote, str(source), limits).items()):
            if name.endswith(f"__{today}.tar"):
                continue
            if name in have:
                already += 1
            else:
                todo.append((name, folder))
    limit = int(cfg["max_days_per_run"])
    for name, folder in todo[:limit]:
        pull(remote, work, folder, limits)
        offsite.upload(folder_id, name, pack(work, folder))
    report = Report(min(len(todo), limit), already, max(len(todo) - limit, 0))
    _log(remote, limits, work, now, report)
    return report


def _log(remote: Remote, limits: Limits, work: Path, now: datetime, report: Report) -> None:
    folder = f"{LOG}/{now:%Y/%m/%d}"
    path = work / folder / f"run-{now:%H%M%S}Z.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"at": now.isoformat(), **report.__dict__}
    path.write_bytes(gzip.compress(json.dumps(body, sort_keys=True).encode(), mtime=0))
    push(work, remote, folder, limits, f"backup: {report.uploaded} pack(s) uploaded")


@dataclass(frozen=True)
class Drill:
    packs: int
    files: int
    mismatched: list[str]


def drill(
    cfg: Mapping[str, Any],
    remote: Remote,
    offsite: Offsite,
    limits: Limits,
    seed: int,
) -> Drill:
    """Download ``drill_files`` packs at random and compare every file with the remote store."""
    have = offsite.list(offsite.folder(str(cfg["folder"])))
    if not have:
        raise BackupError("the backup is empty")
    rng = random.Random(seed)  # noqa: S311 - a reproducible sample, not a secret
    chosen = rng.sample(sorted(have), min(int(cfg["drill_files"]), len(have)))
    files, bad = 0, list[str]()
    for name in chosen:
        for path, data in sorted(unpack(offsite.download(have[name].id)).items()):
            files += 1
            stored = with_retries(lambda p=path: remote.read(p), limits)
            if stored != data:
                bad.append(path)
    return Drill(len(chosen), files, bad)
