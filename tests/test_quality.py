"""Every kind of planted error is found; clean data gives no finding (Phase 2.7 acceptance)."""

from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pytest

from finresearch import quality
from finresearch.core.config import load_toml
from finresearch.core.time import to_epoch_us, utc
from finresearch.data import bars, funding, mark, open_interest
from finresearch.data.fixed import Fixed
from finresearch.data.partitioned import write_partition
from finresearch.data.store import DirectoryRemote, push
from finresearch.data.table import PitTable
from tests.test_revised import LIMITS

CFG = load_toml("quality")
START = utc(2026, 10, 1)
PUBLISHED = utc(2026, 11, 2)
MINUTE_US = 60_000_000


def _minutes(n: int, skip: set[int] | None = None) -> list[int]:
    first = to_epoch_us(START)
    return [first + i * MINUTE_US for i in range(n) if i not in (skip or set())]


def _fixed(values: list[int]) -> Fixed:
    return Fixed(pa.array(values, pa.int64()), 2)  # cents


def _bars(
    exchange: str = "binance",
    symbol: str = "AUSDT",
    n: int = 10,
    close: dict[int, int] | None = None,
    high: dict[int, int] | None = None,
    low: dict[int, int] | None = None,
    skip: set[int] | None = None,
) -> pa.Table:
    """Made-up minutes at 100.00 with small volume; ``close``/``high``/``low`` override minute i."""
    starts = _minutes(n, skip)
    idx = [i for i in range(n) if i not in (skip or set())]
    c = [(close or {}).get(i, 10000) for i in idx]
    high, low = high or {}, low or {}
    h = [high.get(i, max(10000, ci)) for i, ci in zip(idx, c, strict=True)]
    lo = [low.get(i, min(10000, ci)) for i, ci in zip(idx, c, strict=True)]
    values = {
        "open": _fixed([10000] * len(idx)),
        "high": _fixed(h),
        "low": _fixed(lo),
        "close": _fixed(c),
        "volume": _fixed([100] * len(idx)),
        "quote_volume": _fixed([10000] * len(idx)),
        "taker_buy_volume": _fixed([50] * len(idx)),
        "taker_buy_quote_volume": _fixed([5000] * len(idx)),
    }
    trades = pa.array([3] * len(idx), pa.int64())
    return bars.bars(
        exchange, symbol, pa.array(starts, pa.int64()), values, trades, PUBLISHED
    ).table


def _marks(
    n: int = 10, close: dict[int, int] | None = None, skip: set[int] | None = None
) -> pa.Table:
    idx = [i for i in range(n) if i not in (skip or set())]
    prices = _fixed([(close or {}).get(i, 10000) for i in idx])
    samples = pa.array([12] * len(idx), pa.int64())
    starts = pa.array(_minutes(n, skip), pa.int64())
    return mark.marks("binance", "AUSDT", starts, prices, samples, PUBLISHED).table


def _flagged(findings: list[quality.Finding]) -> dict[str, int]:
    return {f.check: f.count for f in findings if f.flagged}


def test_clean_bars_and_marks_give_no_finding() -> None:
    rules = CFG["tables"]
    assert _flagged(quality.bar_findings(_bars(), rules["u1.bars_1m"])) == {}
    assert _flagged(quality.mark_findings(_marks(), rules["u1.mark_1m"])) == {}
    assert quality.schema_finding(_bars().schema, bars.NAME).count == 0


def test_planted_bar_errors_are_each_found() -> None:
    rules = CFG["tables"]["u1.bars_1m"]
    # Minute 2: low at zero. Minute 4: high below the close (impossible). Minute 6: close
    # jumps to 150.00 and minute 7 falls back to 100.00 (two moves of more than 30%).
    # Minutes 8 and 9 are missing from a 12-minute span (2 of 12 steps, above 1%).
    t = _bars(n=12, low={2: 0}, high={4: 9000}, close={6: 15000}, skip={8, 9})
    found = _flagged(quality.bar_findings(t, rules))
    assert found == {
        "range: a price is not positive": 1,
        "range: impossible bars (bars.problems)": 1,
        "range: the close moved more than 30% from the row before": 2,
        "gaps: 1-minute steps missing inside a contract's month": 2,
    }


def test_moves_and_gaps_do_not_cross_from_one_contract_to_the_next() -> None:
    rules = CFG["tables"]["u1.bars_1m"]
    # Two contracts at very different prices, each complete: nothing to find.
    a = _bars(symbol="AUSDT", n=5)
    b = _bars(
        symbol="BUSDT",
        n=5,
        close=dict.fromkeys(range(5), 500),
        high=dict.fromkeys(range(5), 10000),
        low=dict.fromkeys(range(5), 500),
    )
    assert _flagged(quality.bar_findings(pa.concat_tables([b, a]), rules)) == {}


def test_planted_mark_errors_are_each_found() -> None:
    rules = CFG["tables"]["u1.mark_1m"]
    t = _marks(n=12, close={3: 0, 7: 20000}, skip={10})
    found = _flagged(quality.mark_findings(t, rules))
    assert found == {
        "range: a mark price is not positive": 1,
        # 100 -> 0 (-100%), 0 -> 100 (infinite), 100 -> 200 and 200 -> 100.
        "range: the mark price moved more than 30% from the row before": 4,
        "gaps: 1-minute steps missing inside a contract's month": 1,
    }


def _funding(rates: list[int], hours: list[int], times: list[datetime]) -> pa.Table:
    t = funding.funding(
        "binance",
        "AUSDT",
        pa.array([to_epoch_us(x) for x in times], pa.int64()),
        Fixed(pa.array(rates, pa.int64()), 6),
        pa.array(hours, pa.int8()),
        PUBLISHED,
    )
    return t.table


def test_planted_funding_errors_are_each_found() -> None:
    rules = CFG["tables"]["u1.funding"]
    times = [
        utc(2026, 10, 1, 0),
        utc(2026, 10, 1, 8, 0, 0, 5000),
        utc(2026, 10, 1, 16, 30),
        utc(2026, 10, 2, 0),
    ]
    # Rates are in millionths: 100 = 0.01%; 60000 = 6% is beyond the 5% cap. A 5-hour
    # interval does not divide a day. 16:30 is off the hour; 08:00:00.005 is not.
    t = _funding([100, -60000, 100, 100], [8, 8, 8, 5], times)
    assert _flagged(quality.funding_findings(t, rules)) == {
        "range: funding rate beyond ±5.0%": 1,
        "range: funding interval not a whole divisor of 24 hours": 1,
        "range: settlement off the hour": 1,
    }
    clean = _funding(
        [100, -100, 100, 100], [8, 8, 8, 4], [*times[:2], utc(2026, 10, 1, 16), times[3]]
    )
    assert _flagged(quality.funding_findings(clean, rules)) == {}


def test_planted_open_interest_errors_are_each_found() -> None:
    rules = CFG["tables"]["u1.open_interest_5m"]
    first = to_epoch_us(START)
    labels = [first + i * 5 * MINUTE_US for i in (0, 1, 2, 5)]  # readings 3 and 4 missing
    values = {
        n: Fixed(pa.array([1000, -1, 1000, 1000], pa.int64()), 0) for n in open_interest.DECIMALS
    }
    t = open_interest.readings(
        "binance", "AUSDT", pa.array(labels, pa.int64()), 0, values, PUBLISHED
    ).table
    assert _flagged(quality.open_interest_findings(t, rules)) == {
        "range: negative open interest": 1,
        "gaps: 5-minute steps missing inside a contract's month": 2,
    }


def test_a_changed_layout_is_found() -> None:
    t = _bars()
    changed = t.set_column(
        t.schema.get_field_index("trades"), "trades", t.column("trades").cast(pa.int32())
    )
    assert quality.schema_finding(changed.schema, bars.NAME).flagged
    assert quality.schema_finding(t.drop_columns(["trades"]).schema, bars.NAME).flagged
    with pytest.raises(quality.QualityError):
        quality.expected_schema("u1.unknown")


def test_prices_apart_across_exchanges_and_against_the_mark_are_found() -> None:
    binance = _bars(n=20)
    bybit = _bars("bybit", n=20, close={5: 10200})  # 2% above Binance in one minute of 20
    found = quality.cross_exchange(binance, bybit, CFG["cross_exchange"])
    assert (found.count, found.total, found.flagged) == (1, 20, True)
    assert found.example == "AUSDT 2026-10-01 00:05"
    marks = _marks(n=20, close={9: 10500})  # 5% away from the last price
    vs = quality.last_vs_mark(binance, marks, CFG["last_vs_mark"])
    assert (vs.count, vs.total, vs.flagged) == (1, 20, True)
    assert not quality.cross_exchange(binance, _bars("bybit", n=20), CFG["cross_exchange"]).flagged


@pytest.mark.parametrize(
    ("stored", "today", "stale"),
    [
        ([date(2026, 9, 1), date(2026, 10, 1)], date(2026, 11, 15), False),
        ([date(2026, 9, 1)], date(2026, 11, 15), True),  # October should be there by the 8th
        ([date(2026, 9, 1)], date(2026, 11, 5), False),  # but not yet on the 5th
        ([], date(2026, 11, 15), True),
    ],
)
def test_freshness(stored: list[date], today: date, stale: bool) -> None:
    assert quality.freshness(stored, today, 7).flagged is stale


def test_run_checks_each_stored_month_and_reports_privately(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    local = tmp_path / "built"
    october = date(2026, 10, 1)
    for part, rows in (("binance", _bars(n=30)), ("bybit", _bars("bybit", n=30, close={4: 10300}))):
        write_partition(bars.concat([_pit(rows)]), local, 1, october, part, {}, 1000)
    write_partition(_pit_mark(_marks(n=30)), local, 1, october, "binance", {}, 1000)
    push(local, remote, "canonical", LIMITS, "build")
    u1 = load_toml("u1")
    r = quality.run(bars.NAME, remote, LIMITS, tmp_path / "work", date(2026, 11, 15), CFG, u1)
    assert (r.table, r.months, r.fresh.flagged) == (bars.NAME, 1, False)
    assert r.flagged == {
        "across sources: Binance and Bybit 1-minute closes more than 1.0% apart": 1
    }
    stored = sorted(remote.list("quality/u1.bars_1m"))
    assert stored == [
        "quality/u1.bars_1m/v1/2026-11-15/2026-10/report.json",
        "quality/u1.bars_1m/v1/2026-11-15/table/report.json",
    ]
    with pytest.raises(quality.QualityError, match="no partition"):
        quality.run(
            bars.NAME,
            remote,
            LIMITS,
            tmp_path / "w2",
            date(2026, 11, 15),
            CFG,
            u1,
            date(2026, 9, 1),
        )


def _pit(rows: pa.Table) -> PitTable:
    return PitTable(bars.NAME, bars.KEYS, rows)


def _pit_mark(rows: pa.Table) -> PitTable:
    return PitTable(mark.NAME, mark.KEYS, rows)


def test_a_partition_that_does_not_match_its_card_is_refused(tmp_path: Path) -> None:
    from finresearch.data.partitioned import PartitionError, read_month

    remote = DirectoryRemote(tmp_path / "remote")
    local = tmp_path / "built"
    card = write_partition(_pit(_bars()), local, 1, date(2026, 10, 1), "binance", {}, 1000)
    data = next(local.rglob("*.parquet"))
    data.write_bytes(data.read_bytes()[:-1] + b"x")
    push(local, remote, "canonical", LIMITS, "build")
    with pytest.raises(PartitionError, match=card.name):
        read_month(bars.NAME, 1, date(2026, 10, 1), remote, LIMITS, tmp_path / "w")


def test_streaming_a_month_finds_what_checking_it_whole_finds(tmp_path: Path) -> None:
    from finresearch.data.partitioned import month_parts

    remote = DirectoryRemote(tmp_path / "remote")
    local = tmp_path / "built"
    october = date(2026, 10, 1)
    write_partition(_pit(_bars(n=20, close={6: 15000})), local, 1, october, "binance", {}, 1000)
    write_partition(
        _pit(_bars("bybit", n=20, close={3: 10300})), local, 1, october, "bybit", {}, 1000
    )
    write_partition(
        _pit_mark(_marks(n=20, close={9: 10500})), local, 1, october, "binance", {}, 1000
    )
    push(local, remote, "canonical", LIMITS, "build")
    files = month_parts(bars.NAME, 1, october, remote, LIMITS, tmp_path / "w")
    mark_file = month_parts(mark.NAME, 1, october, remote, LIMITS, tmp_path / "w")["binance"]
    streamed = quality.stream_month(bars.NAME, october, files, CFG, mark_file)
    whole = quality.check_month(
        bars.NAME,
        october,
        {p: pq_read(f) for p, f in files.items()},
        CFG,
        {"binance": pq_read(mark_file)},
    )
    assert streamed == whole
    assert {f.check for f in streamed.flagged} == {
        "binance: range: the close moved more than 30% from the row before",
        "across sources: Binance and Bybit 1-minute closes more than 1.0% apart",
        "across sources: Binance last price and mark price more than 2.0% apart",
    }


def pq_read(path: Path) -> pa.Table:
    from finresearch.data.partitioned import read_part

    return read_part(path)
