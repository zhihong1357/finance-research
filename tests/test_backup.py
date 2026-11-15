import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from finresearch import backup, monitor
from finresearch.core.config import load_toml
from finresearch.core.time import utc
from finresearch.data.gdrive import DriveFile
from finresearch.data.raw import RawStore
from finresearch.data.store import DirectoryRemote, Limits, push
from finresearch.revised import specs

LIMITS = Limits(
    max_files_per_commit=50, max_entries_per_folder=9000, retries=0, retry_wait_seconds=0
)
CFG = {
    "enabled": True,
    "added": "2026-11-16T00:00:00Z",
    "folder": "finresearch-backup",
    "max_age_hours": 50,
    "max_days_per_run": 2,
    "drill_files": 2,
    "sources": ["s.one", "s.two"],
}
REPO = Path(__file__).resolve().parents[1]


class FakeOffsite:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def folder(self, name: str) -> str:
        return name

    def list(self, folder_id: str) -> dict[str, DriveFile]:
        return {
            n: DriveFile(n, n, len(c), hashlib.md5(c, usedforsecurity=False).hexdigest())
            for n, c in self.files.items()
        }

    def upload(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        self.files[name] = content
        return self.list(folder_id)[name]

    def download(self, file_id: str) -> bytes:
        return self.files[file_id]


def _remote(tmp_path: Path) -> DirectoryRemote:
    remote = DirectoryRemote(tmp_path / "remote")
    raw = tmp_path / "raw"
    store = RawStore(raw)
    for source in ("s.one", "s.two"):
        for day in (13, 14, 15):
            for hour in (0, 8):
                when = utc(2026, 11, day, hour, 17)
                store.save(
                    source, "resp", "https://example.invalid", when, f"{source}{day}{hour}".encode()
                )
        push(raw, remote, f"raw/{source}", LIMITS, "collect")
    return remote


def test_finished_days_are_packed_once_a_few_at_a_time(tmp_path: Path) -> None:
    remote, offsite = _remote(tmp_path), FakeOffsite()
    now = utc(2026, 11, 15, 3, 37)  # the 15th is not finished yet
    first = backup.run(CFG, remote, offsite, LIMITS, tmp_path / "w1", now)
    assert (first.uploaded, first.already, first.pending) == (2, 0, 2)
    second = backup.run(CFG, remote, offsite, LIMITS, tmp_path / "w2", utc(2026, 11, 15, 3, 38))
    assert (second.uploaded, second.already, second.pending) == (2, 2, 0)
    assert sorted(offsite.files) == [
        "s.one__2026-11-13.tar",
        "s.one__2026-11-14.tar",
        "s.two__2026-11-13.tar",
        "s.two__2026-11-14.tar",
    ]
    # Each run left a log the monitor can find.
    assert monitor.newest_backup(remote, now) == utc(2026, 11, 15, 3, 38)


def test_a_pack_is_reproducible_and_checked_when_unpacked(tmp_path: Path) -> None:
    remote, offsite = _remote(tmp_path), FakeOffsite()
    backup.run(CFG, remote, offsite, LIMITS, tmp_path / "w1", utc(2026, 11, 15, 3, 37))
    again = FakeOffsite()
    backup.run(CFG, remote, again, LIMITS, tmp_path / "w2", utc(2026, 11, 15, 3, 37, 1))
    assert offsite.files == again.files  # same day, same bytes
    files = backup.unpack(offsite.files["s.one__2026-11-13.tar"])
    # Two responses on the 13th, each with its data and meta file.
    assert len(files) == 4
    assert all(p.startswith("raw/s.one/2026/11/13/") for p in files)
    # A pack whose content no longer matches its manifest is refused.
    buf = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(offsite.files["s.one__2026-11-13.tar"])) as src,
        tarfile.open(fileobj=buf, mode="w") as dst,
    ):
        for m in src.getmembers():
            data = src.extractfile(m).read()  # type: ignore[union-attr]
            if m.name != backup.MANIFEST:
                data = data + b"!"
            m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
    with pytest.raises(backup.BackupError, match="manifest"):
        backup.unpack(buf.getvalue())


def test_the_drill_compares_every_file_with_the_store(tmp_path: Path) -> None:
    remote, offsite = _remote(tmp_path), FakeOffsite()
    for minute in (37, 38):
        backup.run(
            CFG, remote, offsite, LIMITS, tmp_path / f"w{minute}", utc(2026, 11, 15, 3, minute)
        )
    d = backup.drill(CFG, remote, offsite, LIMITS, seed=7)
    assert (d.packs, d.files, d.mismatched) == (2, 8, [])
    # A pack that differs from the store is caught.
    name = "s.one__2026-11-13.tar"
    files = backup.unpack(offsite.files[name])
    work = tmp_path / "bad"
    for path, data in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_bytes(data[::-1] if path.endswith(".gz") else data)
    offsite.files = {name: backup.pack(work, "raw/s.one/2026/11/13")}
    bad = backup.drill(CFG, remote, offsite, LIMITS, seed=7)
    assert len(bad.mismatched) == 2


def test_the_monitor_flags_a_backup_that_stopped(tmp_path: Path) -> None:
    remote, offsite = _remote(tmp_path), FakeOffsite()
    backup.run(CFG, remote, offsite, LIMITS, tmp_path / "w", utc(2026, 11, 16, 3, 37))
    ok = monitor.check_backup(CFG, remote, utc(2026, 11, 18, 5))
    late = monitor.check_backup(CFG, remote, utc(2026, 11, 18, 6))  # 50 hours and 23 minutes
    assert [s.stale for s in ok + late] == [False, True]
    assert (
        monitor.check_backup({**CFG, "enabled": False}, remote, utc(2027, 1, 1))[0].stale is False
    )


def test_the_backup_list_covers_every_source_that_cannot_be_fetched_again() -> None:
    import tomllib

    cfg = load_toml("backup")
    enabled = {n for n, s in load_toml("collect")["sources"].items() if s.get("enabled", False)}
    needed = {s.source for s in specs().values()}
    for name in enabled:
        entry = tomllib.loads(
            (REPO / "registry/sources" / f"{name}.toml").read_text(encoding="utf-8")
        )
        if entry["access"] == "snapshot":
            needed.add(name)
    assert needed <= set(cfg["sources"])
