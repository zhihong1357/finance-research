import json
from datetime import datetime
from pathlib import Path

import pytest

from finresearch import revised
from finresearch.core.time import utc
from finresearch.data import builds
from finresearch.data.raw import RawStore
from finresearch.data.store import DirectoryRemote, Limits, push
from finresearch.revised import Spec, Verdict

SPEC = Spec("rates.nyfed", "nyfed.reference_rates", 1, "nyfed")
LIMITS = Limits(
    max_files_per_commit=50,
    max_entries_per_folder=9000,
    retries=0,
    retry_wait_seconds=0,
    rate_limit_retries=0,
    rate_limit_wait_seconds=0,
)
# Made-up rates in the API's format.
ROW = {"type": "SOFR", "percentRate": 4.11, "volumeInBillions": 2000, "revisionIndicator": ""}


def _response(days: list[tuple[str, float]]) -> bytes:
    rows = [{**ROW, "effectiveDate": d, "percentRate": r} for d, r in days]
    return json.dumps({"refRates": rows}).encode()


def _collect(
    remote: DirectoryRemote, root: Path, when: datetime, days: list[tuple[str, float]]
) -> None:
    store = RawStore(root)
    store.save(SPEC.source, "SOFR", "https://example.invalid", when, _response(days))
    push(root, remote, f"raw/{SPEC.source}", LIMITS, "collect")


def test_a_build_is_written_once_and_again_only_when_it_changes(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    raw = tmp_path / "raw"
    _collect(remote, raw, utc(2026, 11, 13, 13, 5), [("2026-11-10", 4.12), ("2026-11-12", 4.15)])
    first = revised.build(SPEC, remote, LIMITS, tmp_path / "w1", utc(2026, 11, 13, 13, 47))
    assert first.card is not None
    assert first.card.built == "20261113T134700Z"
    # Two series (the rate and its volume), two days each.
    assert (first.responses, first.rows, first.pushed) == (1, 4, 2)
    assert sorted(remote.list("canonical/rates.nyfed")) == [
        "canonical/rates.nyfed/v1/builds/2026/20261113T134700Z/nyfed.json",
        "canonical/rates.nyfed/v1/builds/2026/20261113T134700Z/nyfed.parquet",
    ]
    # The next fetch sees the same two days: the latest day again as the latest, so
    # only a later copy of a version already known, which compact drops.
    _collect(remote, raw, utc(2026, 11, 13, 21, 17), [("2026-11-10", 4.12), ("2026-11-12", 4.15)])
    again = revised.build(SPEC, remote, LIMITS, tmp_path / "w2", utc(2026, 11, 13, 21, 47))
    assert (again.card, again.responses, again.pushed) == (None, 2, 0)
    # A new day arrives: a new build, and the old one stays.
    _collect(remote, raw, utc(2026, 11, 16, 13, 5), [("2026-11-12", 4.15), ("2026-11-13", 4.2)])
    third = revised.build(SPEC, remote, LIMITS, tmp_path / "w3", utc(2026, 11, 16, 13, 47))
    assert third.card is not None
    cards = [p for p in remote.list("canonical/rates.nyfed") if p.endswith(".json")]
    assert len(cards) == 2
    card, t = builds.load(
        remote,
        tmp_path / "read",
        builds.latest_card_path(remote, "rates.nyfed", 1, "nyfed", LIMITS) or "",
        LIMITS,
    )
    assert card == third.card
    assert t.num_rows == third.rows
    assert len(card.inputs) == 3


def test_verify_rebuilds_the_latest_build(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    raw = tmp_path / "raw"
    _collect(remote, raw, utc(2026, 11, 13, 13, 5), [("2026-11-10", 4.12), ("2026-11-12", 4.15)])
    revised.build(SPEC, remote, LIMITS, tmp_path / "w1", utc(2026, 11, 13, 13, 47))
    check = revised.verify(SPEC, remote, LIMITS, tmp_path / "v1")
    assert (check.verdict, check.detail) == (Verdict.SAME, "4 rows")
    # A response collected after the build is not one of its inputs and does not count.
    _collect(remote, raw, utc(2026, 11, 16, 13, 5), [("2026-11-13", 4.2)])
    assert revised.verify(SPEC, remote, LIMITS, tmp_path / "v2").verdict is Verdict.SAME


def test_verify_notices_a_missing_response(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    _collect(remote, tmp_path / "raw", utc(2026, 11, 13, 13, 5), [("2026-11-12", 4.15)])
    revised.build(SPEC, remote, LIMITS, tmp_path / "w1", utc(2026, 11, 13, 13, 47))
    remote.remove(f"raw/{SPEC.source}", "lost")
    check = revised.verify(SPEC, remote, LIMITS, tmp_path / "v1")
    assert (check.verdict, check.detail) == (
        Verdict.INPUTS_CHANGED,
        "1 response(s) missing, 0 changed",
    )


def test_a_build_that_does_not_match_its_card_is_refused(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    _collect(remote, tmp_path / "raw", utc(2026, 11, 13, 13, 5), [("2026-11-12", 4.15)])
    made = revised.build(SPEC, remote, LIMITS, tmp_path / "w1", utc(2026, 11, 13, 13, 47))
    assert made.card is not None
    folder = builds.build_dir("rates.nyfed", 1, utc(2026, 11, 13, 13, 47))
    local = tmp_path / "read"
    builds.load(remote, local, f"{folder}/nyfed.json", LIMITS)
    path = local / folder / "nyfed.parquet"
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF  # same size, so the copy is not fetched again
    path.write_bytes(bytes(data))
    with pytest.raises(builds.BuildError, match="does not match its card"):
        builds.load(remote, local, f"{folder}/nyfed.json", LIMITS)


def test_nothing_to_build_and_unknown_tables_are_errors(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    with pytest.raises(revised.RevisedError, match="nothing for"):
        revised.build(SPEC, remote, LIMITS, tmp_path / "w", utc(2026, 11, 13))
    with pytest.raises(revised.RevisedError, match="no build yet"):
        revised.verify(SPEC, remote, LIMITS, tmp_path / "v")
    with pytest.raises(revised.RevisedError, match="not a table"):
        revised.spec("no.such.table")


def test_the_configured_tables_have_adapters() -> None:
    for s in revised.specs().values():
        assert s.source in revised.ADAPTERS
