import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from finresearch.collect import CollectError, collect_config, group_for_cron, plan, run_source
from finresearch.core.time import utc
from finresearch.data.store import DirectoryRemote, Limits
from finresearch.data.table import PitTable
from finresearch.sources import ADAPTERS
from finresearch.sources.base import Adapter, Response, SourceError

REPO = Path(__file__).resolve().parents[1]
CFG = collect_config()
LIMITS = Limits(
    max_files_per_commit=50, max_entries_per_folder=9000, retries=0, retry_wait_seconds=0
)
NOW = utc(2026, 11, 14, 8, 17, 3)


def test_plan_lists_enabled_sources_of_a_group() -> None:
    cfg: dict[str, Any] = {
        "groups": {"g1": {"cron": "1 * * * *"}, "g2": {"cron": "2 * * * *"}},
        "sources": {
            "b": {"group": "g1", "enabled": True},
            "a": {"group": "g1", "enabled": True},
            "off": {"group": "g1", "enabled": False},
            "other": {"group": "g2", "enabled": True},
        },
    }
    assert plan(cfg, "g1") == ["a", "b"]
    assert group_for_cron(cfg, "2 * * * *") == "g2"
    with pytest.raises(CollectError, match="unknown group"):
        plan(cfg, "nope")
    with pytest.raises(CollectError, match="no group has the cron"):
        group_for_cron(cfg, "3 * * * *")


def test_the_configuration_matches_the_workflows_and_the_registry() -> None:
    workflow = (REPO / ".github/workflows/collect.yml").read_text(encoding="utf-8")
    scheduled = set(re.findall(r'cron: "([^"]+)"', workflow))
    assert {g["cron"] for g in CFG["groups"].values()} == scheduled
    for path in CFG["monitor"]["scheduled_workflows"]:
        assert (REPO / path).is_file()
    for name, src in CFG["sources"].items():
        assert name in ADAPTERS
        assert (REPO / "registry/sources" / f"{name}.toml").is_file()
        assert src["group"] in CFG["groups"]


def test_a_run_keeps_the_raw_bytes_and_pushes_them(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    result = run_source("heartbeat.canary", CFG, remote, LIMITS, tmp_path / "work", NOW)
    assert result.responses == 1
    assert sorted(remote.list("raw/heartbeat.canary")) == [
        "raw/heartbeat.canary/2026/11/14/heartbeat-081703Z.json.gz",
        "raw/heartbeat.canary/2026/11/14/heartbeat-081703Z.meta.json",
    ]
    assert remote.log == ["heartbeat.canary: 1 response(s)"]  # one commit


def test_a_failed_fetch_writes_nothing(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    cfg = {
        **CFG,
        "sources": {
            "heartbeat.drill": {**CFG["sources"]["heartbeat.drill"], "simulate_failure": True}
        },
    }
    with pytest.raises(SourceError):
        run_source("heartbeat.drill", cfg, remote, LIMITS, tmp_path / "work", NOW)
    assert remote.log == []


def test_only_enabled_sources_run(tmp_path: Path) -> None:
    cfg = {
        **CFG,
        "sources": {"heartbeat.canary": {**CFG["sources"]["heartbeat.canary"], "enabled": False}},
    }
    with pytest.raises(CollectError, match="not an enabled source"):
        run_source("heartbeat.canary", cfg, DirectoryRemote(tmp_path), LIMITS, tmp_path, NOW)


def test_raw_bytes_are_kept_even_when_parsing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(_cfg: Mapping[str, Any], now: datetime) -> list[Response]:
        return [Response("page", "https://example.invalid", now, b"<html>maintenance</html>")]

    def parse(_content: bytes, _at: datetime) -> PitTable:
        raise ValueError("the format changed")

    monkeypatch.setitem(ADAPTERS, "heartbeat.canary", Adapter("heartbeat.canary", fetch, parse))
    remote = DirectoryRemote(tmp_path / "remote")
    with pytest.raises(ValueError, match="format changed"):
        run_source("heartbeat.canary", CFG, remote, LIMITS, tmp_path / "work", NOW)
    assert len(remote.list("raw/heartbeat.canary")) == 2  # pushed before parsing
