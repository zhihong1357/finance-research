from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from finresearch import acceptance, revised
from finresearch.core.time import utc
from finresearch.data import vintages
from finresearch.data.fixed import parse
from finresearch.data.raw import RawStore
from finresearch.data.store import DirectoryRemote, push
from finresearch.data.table import AVAILABLE_AT, PitTable
from finresearch.data.vintages import Frequency
from tests.test_revised import LIMITS, SPEC, _collect

NAME = "test.accept"
FETCHED = utc(2026, 11, 15)
MAY, JUNE, JULY = utc(2026, 5, 15), utc(2026, 6, 15), utc(2026, 7, 15)


def _series(series: str, start: date, texts: list[str], known: list[datetime]) -> PitTable:
    return vintages.versions(
        NAME, series, Frequency.QUARTER, [start] * len(texts), parse(texts), known, FETCHED
    )


def _table() -> PitTable:
    # Made-up output: the first quarter revised twice (one revision repeats the value,
    # which is not a revision), the fourth quarter of 2025 never revised.
    q1 = _series(
        "ROUTPUT",
        date(2026, 1, 1),
        ["100.0", "100.5", "100.50", "101"],
        [MAY, JUNE, JULY, utc(2026, 8, 1)],
    )
    q4 = _series("ROUTPUT", date(2025, 10, 1), ["99"], [utc(2026, 2, 1)])
    return vintages.concat(NAME, [q1, q4])


def test_rows_that_keep_the_three_time_promise_pass() -> None:
    t = acceptance.three_times(_table())
    assert (t.rows, t.early, t.ok) == (5, 0, True)


def test_a_value_public_before_its_period_ends_is_counted() -> None:
    good = _table().table
    times = good.column(AVAILABLE_AT).to_pylist()
    # Rows 0 and 4: public on 2026-03-31 and 2025-12-31, before their quarters end.
    times[0] = utc(2026, 3, 31)
    times[4] = utc(2025, 12, 31)
    table = good.set_column(
        good.schema.get_field_index(AVAILABLE_AT),
        AVAILABLE_AT,
        pa.array(times, good.schema.field(AVAILABLE_AT).type),
    )
    t = acceptance.three_times(PitTable(NAME, vintages.KEYS, table))
    assert (t.rows, t.early, t.ok) == (5, 2, False)


def test_an_event_may_be_public_at_its_own_instant_but_not_before() -> None:
    from finresearch.sources.releases import BEA_ADAPTER, BEA_ARCHIVE

    page = (
        b'<tr class="release-row"><a href="/news/2026/x">x</a>'
        b'<time datetime="2026-07-30T08:30:00-04:00">x</time></tr>'
    )
    t = BEA_ADAPTER.parse_record(
        page, FETCHED, f"{BEA_ARCHIVE}?field_related_product_target_id=451&page=0"
    )
    assert acceptance.three_times(t).ok


def test_the_door_answers_old_before_a_revision_and_new_at_it() -> None:
    p = acceptance.probe(_table(), sample=10, seed=1)
    # One revised observation; two revisions (100.0 -> 100.5 in June, 100.5 -> 101 in August).
    assert (p.revised, p.probed, p.wrong, p.ok) == (1, 2, 0, True)


def test_a_door_that_shows_a_revision_early_would_be_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Asking "just before" at the revision instant itself sees the new value: both
    # "before" answers are then wrong.
    monkeypatch.setattr(acceptance, "_JUST_BEFORE", timedelta(0))
    p = acceptance.probe(_table(), sample=10, seed=1)
    assert (p.probed, p.wrong) == (2, 2)


def test_the_sample_is_capped_and_a_table_without_revisions_is_fine() -> None:
    assert acceptance.probe(_table(), sample=1, seed=7).probed == 1
    flat = _series("ROUTPUT", date(2025, 10, 1), ["99"], [utc(2026, 2, 1)])
    assert acceptance.probe(flat, sample=10, seed=1) == acceptance.Probe(0, 0, 0)


CFG = {"recorded": ["bea.releases"], "sample": 50}
REVISED = {"tables": {"rates.nyfed": {"source": SPEC.source, "format": 1, "part": "nyfed"}}}


def test_targets_list_revised_tables_then_recorded_sources() -> None:
    assert acceptance.targets(CFG, REVISED) == ["rates.nyfed", "bea.releases"]


def test_run_checks_the_latest_build_of_a_revised_table(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    with pytest.raises(acceptance.AcceptanceError, match="no build yet"):
        acceptance.run("rates.nyfed", remote, LIMITS, tmp_path / "w0", 1, CFG, REVISED)
    _collect(remote, tmp_path / "raw", utc(2026, 11, 13, 13, 5), [("2026-11-12", 4.15)])
    # The next fetch revises the rate of 2026-11-12.
    _collect(remote, tmp_path / "raw", utc(2026, 11, 13, 21, 17), [("2026-11-12", 4.16)])
    revised.build(
        revised.spec("rates.nyfed", REVISED),
        remote,
        LIMITS,
        tmp_path / "w1",
        utc(2026, 11, 13, 21, 47),
    )
    r = acceptance.run("rates.nyfed", remote, LIMITS, tmp_path / "w2", 1, CFG, REVISED)
    assert r.ok
    assert r.probe is not None
    assert (r.probe.revised, r.probe.probed) == (1, 1)


def test_run_parses_every_response_of_a_recorded_source(tmp_path: Path) -> None:
    from finresearch.sources.releases import BEA_ARCHIVE, BEA_SOURCE

    remote = DirectoryRemote(tmp_path / "remote")
    root = tmp_path / "raw"
    page = (
        b'<tr class="release-row"><a href="/news/2026/x">x</a>'
        b'<time datetime="2026-07-30T08:30:00-04:00">x</time></tr>'
    )
    url = f"{BEA_ARCHIVE}?field_related_product_target_id=451&page=0"
    for when in (utc(2026, 11, 16, 6, 41), utc(2026, 11, 23, 6, 41)):
        RawStore(root).save(BEA_SOURCE, "GDP-0", url, when, page)
    push(root, remote, f"raw/{BEA_SOURCE}", LIMITS, "collect")
    r = acceptance.run(BEA_SOURCE, remote, LIMITS, tmp_path / "w", 1, CFG, REVISED)
    assert (r.target, r.times.rows, r.probe, r.ok) == (BEA_SOURCE, 2, None, True)
    with pytest.raises(acceptance.AcceptanceError, match="neither"):
        acceptance.run("nothing", remote, LIMITS, tmp_path / "w", 1, CFG, REVISED)
