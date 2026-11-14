"""Metrics files written by hand in Binance's format, with made-up numbers."""

import pytest

from finresearch.core.time import utc
from finresearch.data.manifest import ManifestEntry
from finresearch.sources.binance_metrics import MetricsFileError, parse_metrics

HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio"
)


def _row(
    time: str, oi: str, value: str, ratios: str = "1.5,2.25,0.75,1.1", sym: str = "AAAUSDT"
) -> str:
    return f"2026-11-11 {time},{sym},{oi},{value},{ratios}"


ROWS = [
    _row("00:05:00", "81686.5980000000000000", "8104837459.5459350000000000", ",2.0,,"),
    _row("00:00:00", "81655.7800000000000000", "8096568534.5260000000000000"),  # out of order
    _row("00:05:00", "81686.5980000000000000", "8104837459.5459350000000000", ",2.0,,"),  # twice
    _row("00:10:00", "0E-16", "0E-16", "0.7554478562331965,,3.00000000,"),
]
URL = "https://x.invalid/data/futures/um/daily/metrics/AAAUSDT/AAAUSDT-metrics-2026-11-11.zip"
DAY = ManifestEntry("binance.um.metrics.daily", "AAAUSDT", "2026-11-11", URL)
PUBLISHED = utc(2026, 11, 12, 1, 8)


def _csv(*rows: str, header: bool = True) -> bytes:
    return ("\n".join([HEADER, *rows] if header else rows) + "\n").encode()


@pytest.mark.parametrize("header", [True, False])
def test_a_metrics_file_becomes_readings(header: bool) -> None:
    t = parse_metrics(_csv(*ROWS, header=header), DAY, PUBLISHED)
    rows = t.table.to_pylist()
    assert [r["event_time"] for r in rows] == [
        utc(2026, 11, 11, 0, 0),
        utc(2026, 11, 11, 0, 5),
        utc(2026, 11, 11, 0, 10),
    ]
    # Available one period after the label, whatever the label means (decision 0011).
    assert [r["available_at"] for r in rows] == [
        utc(2026, 11, 11, 0, 5),
        utc(2026, 11, 11, 0, 10),
        utc(2026, 11, 11, 0, 15),
    ]
    # Padding dropped: three decimals are enough for the quantities of this file...
    assert [(r["open_interest"], r["open_interest_scale"]) for r in rows] == [
        (81_655_780, 3),
        (81_686_598, 3),
        (0, 3),
    ]
    # ...and six for the values: 8104837459.545935.
    assert [(r["open_interest_value"], r["open_interest_value_scale"]) for r in rows] == [
        (8_096_568_534_526_000, 6),
        (8_104_837_459_545_935, 6),
        (0, 6),
    ]
    # A ratio keeps every digit printed; a blank is a missing value.
    assert [(r["top_trader_account_ratio"], r["top_trader_account_ratio_scale"]) for r in rows] == [
        (15_000_000_000_000_000, 16),
        (None, 16),
        (7_554_478_562_331_965, 16),
    ]
    assert [r["top_trader_position_ratio"] for r in rows] == [225, 200, None]
    assert [r["account_ratio"] for r in rows] == [75, None, 300]
    assert "taker_volume_ratio" not in t.table.column_names
    assert {(r["exchange"], r["symbol"], r["ingested_at"]) for r in rows} == {
        ("binance", "AAAUSDT", PUBLISHED)
    }


def test_a_time_off_the_five_minute_marks_is_kept_as_written() -> None:
    # Some regenerated files label readings like this.
    t = parse_metrics(_csv(_row("00:05:46", "1", "2")), DAY, PUBLISHED)
    (row,) = t.table.to_pylist()
    assert (row["event_time"], row["available_at"]) == (
        utc(2026, 11, 11, 0, 5, 46),
        utc(2026, 11, 11, 0, 10, 46),
    )


def test_a_file_may_end_with_the_next_midnight() -> None:
    # Files that label a reading with the time it was taken run from 00:05 to 24:00.
    rows = [_row("23:55:00", "1", "2"), _row("00:00:00", "3", "4").replace("11-11", "11-12")]
    t = parse_metrics(_csv(*rows), DAY, PUBLISHED)
    assert t.table.column("event_time").to_pylist() == [
        utc(2026, 11, 11, 23, 55),
        utc(2026, 11, 12, 0, 0),
    ]
    with pytest.raises(MetricsFileError, match="outside 2026-11-11"):
        parse_metrics(_csv(_row("00:05:00", "3", "4").replace("11-11", "11-12")), DAY, PUBLISHED)


def test_an_empty_day_has_no_rows() -> None:
    assert parse_metrics(_csv(), DAY, PUBLISHED).num_rows == 0


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row("23:55:00", "1", "2").replace("2026-11-11", "2026-11-10")], "outside 2026-11-11"),
        ([_row("00:00:00", "1", "2", sym="BBBUSDT")], "not AAAUSDT"),
        ([_row("00:00:00", "1", "2"), _row("00:00:00", "1", "3")], "repeated or out of order"),
        ([_row("midnight", "1", "2")], "not a time"),
        ([_row("00:00:00", "", "2")], "not a plain decimal"),
        ([_row("00:00:00", "many", "2")], "not a plain decimal"),
        (["2026-11-11 00:00:00,AAAUSDT,1"], "CSV"),
    ],
)
def test_bad_files_are_refused(rows: list[str], message: str) -> None:
    with pytest.raises(MetricsFileError, match=message):
        parse_metrics(_csv(*rows), DAY, PUBLISHED)
