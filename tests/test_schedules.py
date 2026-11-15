from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import pytest

from finresearch.core.config import CONFIG_DIR, load_toml
from finresearch.core.time import utc
from finresearch.schedules import Cron, Due, Run, ScheduleError, catch_up, due, ran, workflow_crons

SUNDAY = utc(2026, 11, 15, 12, 58)  # 2026-11-15 is a Sunday


@pytest.mark.parametrize(
    ("cron", "now", "expected"),
    [
        ("23 */4 * * *", SUNDAY, utc(2026, 11, 15, 12, 23)),
        ("23 */4 * * *", utc(2026, 11, 15, 12, 22, 59), utc(2026, 11, 15, 8, 23)),
        (
            "23 */4 * * *",
            utc(2026, 11, 15, 12, 23),
            utc(2026, 11, 15, 12, 23),
        ),  # due at the instant
        ("53 1 * * *", utc(2026, 11, 15, 1, 0), utc(2026, 11, 14, 1, 53)),
        ("41 6 * * 1", SUNDAY, utc(2026, 11, 9, 6, 41)),  # last Monday
        ("47 20 * * 1,2,5", SUNDAY, utc(2026, 11, 13, 20, 47)),  # Friday
        ("47 4 15 * *", utc(2026, 11, 15, 4, 46), utc(2026, 10, 15, 4, 47)),  # monthly
        ("0 0 * * 0", SUNDAY, utc(2026, 11, 15)),  # 0 is Sunday
        ("0 0 * * 7", SUNDAY, utc(2026, 11, 15)),  # and so is 7
        ("0 0 1 * 5", SUNDAY, utc(2026, 11, 13)),  # day and weekday both set: either matches
        ("10-20/5 9 * * *", SUNDAY, utc(2026, 11, 15, 9, 20)),
    ],
)
def test_the_latest_due_instant(cron: str, now: datetime, expected: datetime) -> None:
    assert Cron.parse(cron).previous(now) == expected


@pytest.mark.parametrize(
    "cron", ["61 * * * *", "* * * *", "0 25 * * *", "*/0 * * * *", "5-1 * * * *"]
)
def test_unreadable_crons_are_refused(cron: str) -> None:
    with pytest.raises(ScheduleError):
        Cron.parse(cron)


def test_the_repository_workflows_have_readable_crons() -> None:
    cfg = load_toml("schedules")
    for name in cfg["workflows"]:
        for cron in workflow_crons(CONFIG_DIR.parent / ".github" / "workflows" / name):
            Cron.parse(cron)


def _repo(tmp_path: Path) -> Path:
    folder = tmp_path / ".github" / "workflows"
    folder.mkdir(parents=True)
    (folder / "collect.yml").write_text(
        'on:\n  schedule:\n    - cron: "23 */4 * * *"   # every4h\n    - cron: "41 6 * * 1"\n',
        encoding="utf-8",
    )
    (folder / "build.yml").write_text(
        'on:\n  schedule:\n    - cron: "47 */8 * * *"\n', encoding="utf-8"
    )
    return tmp_path


CFG = {
    "grace_minutes": 45,
    "lookback_hours": 48,
    "workflows": {"collect.yml": {"by_group": True}, "build.yml": {"inputs": {"mode": "build"}}},
}
GROUPS = {"23 */4 * * *": "every4h", "41 6 * * 1": "weekly"}


def test_workflow_crons_are_read_from_the_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert workflow_crons(root / ".github/workflows/collect.yml") == ["23 */4 * * *", "41 6 * * 1"]
    (root / "empty.yml").write_text("on: push\n", encoding="utf-8")
    with pytest.raises(ScheduleError, match="no cron"):
        workflow_crons(root / "empty.yml")


def test_due_waits_out_the_grace_and_drops_old_instants(tmp_path: Path) -> None:
    # At 12:58 the 12:23 run is only 35 minutes late: the 08:23 one is the last missed.
    # The weekly cron was due 6 days ago, outside 48 hours.
    found = due(CFG, _repo(tmp_path), SUNDAY, GROUPS)
    assert found == [
        Due("build.yml", "47 */8 * * *", None, utc(2026, 11, 15, 8, 47)),
        Due("collect.yml", "23 */4 * * *", "every4h", utc(2026, 11, 15, 8, 23)),
    ]


def test_a_cron_without_a_group_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ScheduleError, match="no group"):
        due(CFG, _repo(tmp_path), SUNDAY, {"41 6 * * 1": "weekly"})


def test_which_runs_count() -> None:
    d = Due("collect.yml", "23 */4 * * *", "every4h", utc(2026, 11, 15, 8, 23))
    late = utc(2026, 11, 15, 10, 2)
    assert ran(d, [Run("采集 23 */4 * * *", late, "success")])  # the scheduled run, late
    assert ran(d, [Run("采集 every4h", late, None)])  # a started run, still going
    assert not ran(d, [Run("采集 every8h", late, "success")])  # another group
    assert not ran(d, [Run("采集 every4h", utc(2026, 11, 15, 8, 22), "success")])  # before due
    assert not ran(d, [Run("采集 every4h", late, "cancelled")])
    assert not ran(d, [Run("采集", late, "success")])  # an old run without the group in its title
    plain = Due("build.yml", "47 */8 * * *", None, utc(2026, 11, 15, 8, 47))
    assert ran(plain, [Run("修订版本表", utc(2026, 11, 15, 9), "failure")])  # a failed run did run


class FakeActions:
    def __init__(self, runs: dict[str, list[Run]]) -> None:
        self._runs = runs
        self.started: list[tuple[str, str, dict[str, str]]] = []
        self.asked: list[tuple[str, datetime]] = []

    def runs(self, workflow: str, since: datetime) -> list[Run]:
        self.asked.append((workflow, since))
        return self._runs.get(workflow, [])

    def dispatch(self, workflow: str, ref: str, inputs: Mapping[str, str]) -> None:
        self.started.append((workflow, ref, dict(inputs)))


def test_catch_up_starts_only_what_was_missed(tmp_path: Path) -> None:
    actions = FakeActions(
        {"collect.yml": [Run("采集 23 */4 * * *", utc(2026, 11, 15, 9, 1), "success")]}
    )
    lines = catch_up(CFG, _repo(tmp_path), actions, SUNDAY, GROUPS, "main")
    assert actions.started == [("build.yml", "main", {"mode": "build"})]
    assert actions.asked == [
        ("build.yml", utc(2026, 11, 15, 8, 47)),
        ("collect.yml", utc(2026, 11, 15, 8, 23)),
    ]
    assert lines == [
        "build.yml (due 2026-11-15 08:47 UTC): missed, started",
        "collect.yml every4h (due 2026-11-15 08:23 UTC): ran",
    ]


def test_catch_up_names_the_group_it_starts(tmp_path: Path) -> None:
    actions = FakeActions({"build.yml": [Run("x", utc(2026, 11, 15, 9), "success")]})
    catch_up(CFG, _repo(tmp_path), actions, SUNDAY, GROUPS, "main")
    assert actions.started == [("collect.yml", "main", {"group": "every4h"})]
