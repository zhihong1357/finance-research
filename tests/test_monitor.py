from datetime import datetime, timedelta
from pathlib import Path

from finresearch.core.time import utc
from finresearch.data.store import DirectoryRemote
from finresearch.github import Issue, Workflow
from finresearch.monitor import (
    check_sources,
    inactivity_alert,
    keepalive,
    newest_record,
    record_time,
    run,
    source_alerts,
    sync_issues,
)

NOW = utc(2026, 12, 2, 12)
CFG = {
    "sources": {
        "fresh": {"max_age_hours": 17, "added": "2026-11-01T00:00:00Z", "enabled": True},
        "stale": {"max_age_hours": 17, "added": "2026-11-01T00:00:00Z", "enabled": True},
        "new": {"max_age_hours": 17, "added": "2026-12-02T00:00:00Z", "enabled": True},
        "off": {"max_age_hours": 17, "added": "2026-11-01T00:00:00Z", "enabled": False},
    },
    "monitor": {
        "issue_marker": "finresearch:outage",
        "inactivity_warn_days": 45,
        "scheduled_workflows": [".github/workflows/collect.yml", ".github/workflows/monitor.yml"],
    },
}


def _record(remote: DirectoryRemote, source: str, at: datetime) -> None:
    path = remote.root / f"raw/{source}/{at:%Y/%m/%d}/heartbeat-{at:%H%M%S}Z.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    path.with_name(path.name.replace(".json.gz", ".meta.json")).write_bytes(b"{}")


class FakeRepo:
    def __init__(self, issues: list[Issue] | None = None, pushed: datetime = NOW) -> None:
        self.issues = {i.number: i for i in issues or []}
        self.comments: list[tuple[int, str]] = []
        self.enabled: list[int] = []
        self.pushed = pushed

    def open_issues(self) -> list[Issue]:
        return list(self.issues.values())

    def create_issue(self, title: str, body: str) -> int:
        number = max(self.issues, default=0) + 1
        self.issues[number] = Issue(number, title, body)
        return number

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def close_issue(self, number: int) -> None:
        del self.issues[number]

    def workflows(self) -> list[Workflow]:
        return [
            Workflow(1, ".github/workflows/ci.yml", "active"),
            Workflow(2, ".github/workflows/collect.yml", "disabled_inactivity"),
            Workflow(3, ".github/workflows/monitor.yml", "active"),
        ]

    def enable_workflow(self, workflow_id: int) -> None:
        self.enabled.append(workflow_id)

    def pushed_at(self) -> datetime:
        return self.pushed


def test_record_times_come_from_paths() -> None:
    assert record_time("raw/s/2026/11/14/heartbeat-081703Z.json.gz") == utc(2026, 11, 14, 8, 17, 3)
    assert record_time("raw/s/2026/11/14/heartbeat-081703Z.meta.json") is None
    assert record_time("raw/s/notes.txt") is None


def test_the_newest_record_looks_back_one_month(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path)
    assert newest_record(remote, "s", NOW) is None
    _record(remote, "s", utc(2026, 11, 30, 23))
    _record(remote, "s", utc(2026, 11, 3, 1))
    assert newest_record(remote, "s", NOW) == utc(2026, 11, 30, 23)  # nothing yet in December
    _record(remote, "s", utc(2026, 12, 1, 7))
    assert newest_record(remote, "s", NOW) == utc(2026, 12, 1, 7)


def test_staleness_by_source(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path)
    _record(remote, "fresh", NOW - timedelta(hours=16))
    _record(remote, "stale", NOW - timedelta(hours=18))
    status = {s.source: s for s in check_sources(CFG, remote, NOW)}
    assert not status["fresh"].stale
    assert status["stale"].stale
    assert not status["new"].stale  # added 12 hours ago, no data yet: still within its grace
    assert not status["off"].stale
    assert not status["off"].enabled
    (alert,) = source_alerts(list(status.values()), NOW)
    assert alert.key == "source:stale"
    assert "18 小时没有新数据" in alert.body


def test_issues_follow_the_alerts() -> None:
    repo = FakeRepo([Issue(7, "unrelated", "written by a person")])
    alert = source_alerts(check_sources(CFG, DirectoryRemote(Path("/nonexistent")), NOW), NOW)
    stale_keys = sorted(a.key for a in alert)
    assert stale_keys == ["source:fresh", "source:stale"]  # neither has any record
    assert sync_issues(alert, repo, "finresearch:outage", NOW) == [
        "opened #8: source:fresh",
        "opened #9: source:stale",
    ]
    assert sync_issues(alert, repo, "finresearch:outage", NOW) == []  # no duplicates
    assert sync_issues(alert[1:], repo, "finresearch:outage", NOW) == ["closed #8: source:fresh"]
    assert repo.comments[0][0] == 8
    assert sorted(repo.issues) == [7, 9]  # the person's issue is never touched


def test_inactivity_warning() -> None:
    assert inactivity_alert(NOW - timedelta(days=44, hours=23), NOW, 45) is None
    alert = inactivity_alert(NOW - timedelta(days=45), NOW, 45)
    assert alert is not None
    assert alert.key == "inactivity"


def test_keepalive_enables_the_scheduled_workflows() -> None:
    repo = FakeRepo()
    lines = keepalive(repo, [".github/workflows/collect.yml", ".github/workflows/missing.yml"])
    assert repo.enabled == [2]
    assert lines == [
        ".github/workflows/collect.yml: was disabled_inactivity, enabled",
        ".github/workflows/missing.yml: not found",
    ]


def test_one_monitor_pass(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path)
    _record(remote, "fresh", NOW - timedelta(hours=1))
    _record(remote, "stale", NOW - timedelta(days=2))
    repo = FakeRepo(pushed=NOW - timedelta(days=50))
    lines = run(CFG, remote, repo, NOW)
    assert "stale: STALE (newest 2026-11-30 12:00 UTC)" in lines
    assert "opened #1: source:stale" in lines
    assert "opened #2: inactivity" in lines
    assert repo.enabled == [2, 3]
