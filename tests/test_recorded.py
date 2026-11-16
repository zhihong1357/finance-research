import json
from datetime import date, datetime
from pathlib import Path

import pytest

from finresearch import recorded
from finresearch.core.time import to_epoch_ms, utc
from finresearch.data.partitioned import PartitionedTable
from finresearch.data.pit import Clock, PointInTime
from finresearch.data.raw import RawStore
from finresearch.data.store import DirectoryRemote, push
from finresearch.sources.deribit_dvol import URL
from tests.test_revised import LIMITS

HOUR = 3_600_000
DVOL = recorded.Spec("deribit.dvol", "deribit.dvol", 1, "deribit", 3, True, date(2026, 9, 1))
HL = recorded.Spec(
    "hyperliquid.asset_ctxs",
    "hyperliquid.asset_ctxs",
    1,
    "hyperliquid",
    2,
    False,
    date(2026, 10, 1),
)
ROWS = 1000


def _page(candles: list[tuple[datetime, int]]) -> bytes:
    """A made-up DVOL page: hourly candles (start, close), newest first."""
    data = [[to_epoch_ms(t), c, c, c, c] for t, c in sorted(candles, reverse=True)]
    return json.dumps({"jsonrpc": "2.0", "result": {"data": data, "continuation": None}}).encode()


DVOL_URL = f"{URL}?currency=BTC&start_timestamp=0&end_timestamp=1&resolution=3600"


def _save_dvol(root: Path, fetched_at: datetime, candles: list[tuple[datetime, int]]) -> None:
    RawStore(root).save(
        "deribit.dvol", f"BTC-3600-{fetched_at:%H%M%S}", DVOL_URL, fetched_at, _page(candles)
    )


def test_a_backfill_is_sorted_into_the_months_of_its_candles(tmp_path: Path) -> None:
    # One response fetched in November holds candles of September and October.
    _save_dvol(
        tmp_path, utc(2026, 11, 16, 1, 53), [(utc(2026, 9, 30, 22), 50), (utc(2026, 10, 1, 0), 51)]
    )
    store = RawStore(tmp_path)
    months = recorded.sort_into_months(DVOL, store, store.records("deribit.dvol"))
    assert sorted(months) == [date(2026, 9, 1), date(2026, 10, 1)]
    assert [t.num_rows for t in months[date(2026, 9, 1)].tables] == [1]
    (path,) = months[date(2026, 10, 1)].inputs
    assert path.startswith("raw/deribit.dvol/2026/11/16/")


def test_a_candle_fetched_twice_is_kept_as_first_seen(tmp_path: Path) -> None:
    candle = utc(2026, 10, 31, 22)
    _save_dvol(tmp_path, utc(2026, 11, 1, 1, 53), [(candle, 50)])
    _save_dvol(tmp_path, utc(2026, 11, 2, 1, 53), [(candle, 50), (utc(2026, 10, 31, 23), 52)])
    _save_dvol(tmp_path, utc(2026, 11, 3, 1, 53), [(candle, 49)])  # a repeat that disagrees
    store = RawStore(tmp_path)
    month = recorded.sort_into_months(DVOL, store, store.records("deribit.dvol"))[date(2026, 10, 1)]
    table, disagreed = recorded.first_seen(month.tables)
    rows = table.table.to_pylist()
    assert [(r["event_time"], r["ingested_at"]) for r in rows] == [
        (candle, utc(2026, 11, 1, 1, 53)),
        (utc(2026, 10, 31, 23), utc(2026, 11, 2, 1, 53)),
    ]
    assert disagreed == 1


@pytest.mark.parametrize(
    ("now", "ready"),
    [
        (utc(2026, 11, 3, 23, 59), False),
        (utc(2026, 11, 4), True),
    ],  # October ends 11-01, plus 3 days
)
def test_a_month_settles_some_days_after_it_ends(now: datetime, ready: bool) -> None:
    assert recorded.settled(date(2026, 10, 1), now, 3) is ready


def test_build_writes_each_settled_month_once_and_verify_rebuilds_it(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    raw = tmp_path / "raw"
    _save_dvol(
        raw,
        utc(2026, 11, 2, 1, 53),
        [(utc(2026, 9, 15), 50), (utc(2026, 10, 31, 23), 52), (utc(2026, 11, 1), 53)],
    )
    push(raw, remote, "raw", LIMITS, "collect")
    made = recorded.build(DVOL, remote, LIMITS, tmp_path / "w1", utc(2026, 11, 5), ROWS)
    assert (made.responses, made.months_written, made.months_waiting) == (
        1,
        ["2026-09", "2026-10"],
        ["2026-11"],
    )
    again = recorded.build(DVOL, remote, LIMITS, tmp_path / "w2", utc(2026, 11, 5), ROWS)
    assert (again.months_written, again.pushed) == ([], 0)
    assert recorded.verify(DVOL, date(2026, 10, 1), remote, LIMITS, tmp_path / "w3", ROWS) == "same"


def test_verify_notices_a_rule_that_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    raw = tmp_path / "raw"
    _save_dvol(raw, utc(2026, 11, 2, 1, 53), [(utc(2026, 10, 30), 50), (utc(2026, 10, 31), 52)])
    push(raw, remote, "raw", LIMITS, "collect")
    recorded.build(DVOL, remote, LIMITS, tmp_path / "w1", utc(2026, 11, 5), ROWS)
    original = recorded.first_seen

    def drop_last(tables: list[recorded.PitTable]) -> tuple[recorded.PitTable, int]:
        t, n = original(tables)
        return recorded.PitTable(t.name, t.keys, t.table.slice(0, t.num_rows - 1)), n

    monkeypatch.setattr(recorded, "first_seen", drop_last)
    assert (
        recorded.verify(DVOL, date(2026, 10, 1), remote, LIMITS, tmp_path / "w2", ROWS) == "changed"
    )
    with pytest.raises(recorded.RecordedError, match="no partition"):
        recorded.verify(DVOL, date(2026, 8, 1), remote, LIMITS, tmp_path / "w3", ROWS)


def _snapshot(coins: list[str]) -> bytes:
    universe = {"universe": [{"name": c} for c in coins]}
    contexts = [{"markPx": "1.5", "openInterest": "10", "funding": "0.0001"} for _ in coins]
    return json.dumps([universe, contexts]).encode()


def test_snapshots_build_from_their_own_month_and_read_through_the_door(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    raw = tmp_path / "raw"
    url = "https://api.hyperliquid.xyz/info"
    for when in (utc(2026, 10, 5, 4, 23), utc(2026, 10, 5, 8, 23), utc(2026, 11, 1, 0, 23)):
        RawStore(raw).save(HL.source, "metaAndAssetCtxs", url, when, _snapshot(["BTC", "ETH"]))
    push(raw, remote, "raw", LIMITS, "collect")
    made = recorded.build(HL, remote, LIMITS, tmp_path / "w1", utc(2026, 11, 3), ROWS)
    # Only October's responses are pulled: November has not settled.
    assert (made.responses, made.months_written) == (2, ["2026-10"])
    table = PartitionedTable(HL.table, ("coin",), 1, tmp_path / "door", remote, LIMITS)
    door = PointInTime(Clock(limit=utc(2026, 10, 31, 23, 59)), [table])
    snap = door.window(HL.table, utc(2026, 10, 5), utc(2026, 10, 5, 5))
    assert sorted(r["coin"] for r in snap.table.to_pylist()) == [
        "BTC",
        "ETH",
    ]  # the 04:23 snapshot only


def test_verify_can_reuse_one_raw_layer_across_months(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    raw = tmp_path / "raw"
    _save_dvol(raw, utc(2026, 11, 2, 1, 53), [(utc(2026, 9, 15), 50), (utc(2026, 10, 31, 23), 52)])
    push(raw, remote, "raw", LIMITS, "collect")
    recorded.build(DVOL, remote, LIMITS, tmp_path / "w1", utc(2026, 11, 5), ROWS)
    shared = tmp_path / "shared"
    for i, month in enumerate((date(2026, 9, 1), date(2026, 10, 1))):
        assert (
            recorded.verify(DVOL, month, remote, LIMITS, tmp_path / f"v{i}", ROWS, shared) == "same"
        )
    assert len(list(shared.rglob("*.json.gz"))) == 1  # pulled once, used for both months
