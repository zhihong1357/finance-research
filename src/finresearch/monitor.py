"""The monitor (Phase 2.2): data freshness, outage issues, and keeping schedules alive.

Every few hours the monitor works out which alerts *should* be open and makes
the repository's issues match:

* **stale source**: an enabled source whose newest record in the remote store
  is older than its ``max_age_hours`` (or, for a source without records yet,
  whose ``added`` time is that old). Archive datasets (``config/archives.toml``)
  count as sources: their record is the newest manifest increment;
* **inactivity**: no push to the repository for ``inactivity_warn_days``.
  GitHub disables scheduled workflows in a public repository after 60 days
  without activity, so the owner is warned while there is still time.

An alert that should be open and is not gets a new issue (GitHub emails the
owner). An issue for an alert that has cleared gets a comment and is closed.
Each issue carries a hidden marker naming its alert, so the monitor only ever
touches issues it opened.

The monitor also asks GitHub to enable each scheduled workflow. Whether that
resets the 60-day clock is not documented (docs/architecture.md 7.4); the
inactivity alert does not depend on it.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from finresearch.core.time import ensure_utc, month_start, parse_utc, utc
from finresearch.data.manifest import PREFIX, increment_time
from finresearch.data.store import Remote
from finresearch.github import Issue, Workflow

_RECORD = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/[^/]+-(\d{2})(\d{2})(\d{2})Z\.json\.gz$")
_SECONDS_PER_HOUR = 3600


class Tracker(Protocol):
    def open_issues(self) -> list[Issue]: ...
    def create_issue(self, title: str, body: str) -> int: ...
    def comment(self, number: int, body: str) -> None: ...
    def close_issue(self, number: int) -> None: ...


class Repo(Tracker, Protocol):
    def workflows(self) -> list[Workflow]: ...
    def enable_workflow(self, workflow_id: int) -> None: ...
    def pushed_at(self) -> datetime: ...


def record_time(path: str) -> datetime | None:
    """When a raw record was fetched, from its path: ``.../YYYY/MM/DD/name-HHMMSSZ.json.gz``."""
    m = _RECORD.search(path)
    return utc(*(int(g) for g in m.groups())) if m else None


def newest_record(remote: Remote, source: str, now: datetime) -> datetime | None:
    """The newest record of ``source`` this month or last month, if any."""
    for offset in (0, -1):
        month = month_start(ensure_utc(now).date(), offset)
        times = [
            t
            for p in remote.list(f"raw/{source}/{month:%Y/%m}")
            if (t := record_time(p)) is not None
        ]
        if times:
            return max(times)
    return None


def newest_increment(remote: Remote, dataset: str) -> datetime | None:
    """When the newest manifest increment of an archive dataset was listed, if any."""
    times = [t for p in remote.list(f"{PREFIX}/{dataset}") if (t := increment_time(p)) is not None]
    return max(times, default=None)


@dataclass(frozen=True, slots=True)
class Alert:
    key: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source: str
    enabled: bool
    newest: datetime | None
    max_age: timedelta
    stale: bool


def _fmt(t: datetime | None) -> str:
    return f"{t:%Y-%m-%d %H:%M} UTC" if t is not None else "还没有任何记录"


def check_sources(cfg: Mapping[str, Any], remote: Remote, now: datetime) -> list[SourceStatus]:
    out: list[SourceStatus] = []
    for name, src in sorted(cfg["sources"].items()):
        max_age = timedelta(hours=float(src["max_age_hours"]))
        if not src.get("enabled", False):
            out.append(SourceStatus(name, False, None, max_age, stale=False))
            continue
        newest = newest_record(remote, name, now)
        since = newest or parse_utc(str(src["added"]))
        out.append(SourceStatus(name, True, newest, max_age, stale=now - since > max_age))
    return out


def check_archives(
    archives: Mapping[str, Any], remote: Remote, now: datetime
) -> list[SourceStatus]:
    """Each archive dataset is stale when no manifest increment arrived within the limit."""
    max_age = timedelta(hours=float(archives["monitor"]["max_age_hours"]))
    out: list[SourceStatus] = []
    for name, spec in sorted(archives["datasets"].items()):
        if not spec.get("enabled", False):
            out.append(SourceStatus(name, False, None, max_age, stale=False))
            continue
        newest = newest_increment(remote, name)
        since = newest or parse_utc(str(spec["added"]))
        out.append(SourceStatus(name, True, newest, max_age, stale=now - since > max_age))
    return out


def source_alerts(statuses: list[SourceStatus], now: datetime) -> list[Alert]:
    alerts: list[Alert] = []
    for s in statuses:
        if not s.stale:
            continue
        gap = (
            "自加入以来还没有任何数据"
            if s.newest is None
            else f"已经 {(now - s.newest).total_seconds() / _SECONDS_PER_HOUR:.0f} 小时没有新数据"
        )
        alerts.append(
            Alert(
                f"source:{s.source}",
                f"数据中断：{s.source}",
                "\n".join(
                    [
                        f"数据源 `{s.source}` {gap}，"
                        f"上限是 {s.max_age.total_seconds() / _SECONDS_PER_HOUR:.0f} 小时。",
                        "",
                        f"- 最新一条记录：{_fmt(s.newest)}",
                        f"- 检查时间：{_fmt(now)}",
                        "",
                        "这个问题由监控任务自动打开，数据恢复后会自动关闭。"
                        "处理办法见 `docs/development.md` 的“收到数据中断通知时”。",
                    ]
                ),
            )
        )
    return alerts


def inactivity_alert(pushed_at: datetime, now: datetime, warn_days: int) -> Alert | None:
    idle = (now - pushed_at).days
    if idle < warn_days:
        return None
    return Alert(
        "inactivity",
        "提醒：仓库将近 60 天没有活动，定时任务会被 GitHub 停用",
        "\n".join(
            [
                f"仓库最后一次推送是 {_fmt(pushed_at)}，已经 {idle} 天。"
                "公开仓库 60 天没有活动时，GitHub 会自动停用全部定时任务，采集和监控都会停下。",
                "",
                "处理办法：合并任意一个拉取请求即可。如果已经被停用，在仓库的 Actions 页面"
                "分别打开“采集”和“监控”，点 Enable workflow。",
            ]
        ),
    )


def _marker(prefix: str, key: str) -> str:
    return f"<!-- {prefix} key={key} -->"


def sync_issues(alerts: list[Alert], tracker: Tracker, prefix: str, now: datetime) -> list[str]:
    """Open an issue for each new alert; close the monitor's issues whose alert cleared."""
    pattern = re.compile(re.escape(f"<!-- {prefix} key=") + r"(\S+) -->")
    mine: dict[str, Issue] = {}
    for issue in tracker.open_issues():
        if m := pattern.search(issue.body):
            mine.setdefault(m.group(1), issue)
    wanted = {a.key: a for a in alerts}
    done: list[str] = []
    for key, alert in wanted.items():
        if key not in mine:
            number = tracker.create_issue(alert.title, f"{_marker(prefix, key)}\n{alert.body}")
            done.append(f"opened #{number}: {key}")
    for key, issue in sorted(mine.items()):
        if key not in wanted:
            tracker.comment(issue.number, f"监控在 {_fmt(now)} 检查时已恢复正常，自动关闭。")
            tracker.close_issue(issue.number)
            done.append(f"closed #{issue.number}: {key}")
    return done


def keepalive(repo: Repo, scheduled: list[str]) -> list[str]:
    """Ask GitHub to enable every scheduled workflow; report what each was before."""
    done: list[str] = []
    found = {w.path: w for w in repo.workflows()}
    for path in scheduled:
        w = found.get(path)
        if w is None:
            done.append(f"{path}: not found")
            continue
        repo.enable_workflow(w.id)
        done.append(f"{path}: was {w.state}, enabled")
    return done


def run(
    cfg: Mapping[str, Any],
    archives: Mapping[str, Any],
    remote: Remote,
    repo: Repo,
    now: datetime,
) -> list[str]:
    """One monitor pass. Returns log lines (sizes, times and states only, never data)."""
    mon = cfg["monitor"]
    statuses = check_sources(cfg, remote, now) + check_archives(archives, remote, now)
    lines = [
        f"{s.source}: {'disabled' if not s.enabled else ('STALE' if s.stale else 'ok')}"
        f" (newest {_fmt(s.newest)})"
        for s in statuses
    ]
    alerts = source_alerts(statuses, now)
    idle = inactivity_alert(repo.pushed_at(), now, int(mon["inactivity_warn_days"]))
    if idle is not None:
        alerts.append(idle)
    lines += sync_issues(alerts, repo, str(mon["issue_marker"]), now)
    lines += keepalive(repo, [str(p) for p in mon["scheduled_workflows"]])
    return lines
