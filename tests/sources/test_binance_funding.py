"""Funding files written by hand in Binance's format, with made-up rates."""

import pytest

from finresearch.core.time import utc
from finresearch.data.manifest import ManifestEntry
from finresearch.sources.binance_funding import FundingFileError, parse_funding

HEADER = "calc_time,funding_interval_hours,last_funding_rate"
# 2026-10-01 00:00 UTC is 1,790,812,800 seconds after the epoch (20727 days).
MS = 1_790_812_800_000
HOUR = 3_600_000
ROWS = [
    f"{MS + 8 * HOUR + 3},8,-7.4E-7",  # 3 ms after the hour; tiny rates come in this notation
    f"{MS},8,0.00010000",  # out of order
    f"{MS + 16 * HOUR},4,0.00012345",
]
URL = (
    "https://x.invalid/data/futures/um/monthly/fundingRate/AAAUSDT/AAAUSDT-fundingRate-2026-10.zip"
)
MONTH = ManifestEntry("binance.um.funding.monthly", "AAAUSDT", "2026-10", URL)
PUBLISHED = utc(2026, 11, 2, 3, 4)


def _csv(*rows: str, header: bool = True) -> bytes:
    return ("\n".join([HEADER, *rows] if header else rows) + "\n").encode()


@pytest.mark.parametrize("header", [True, False])
def test_a_funding_file_becomes_settlements(header: bool) -> None:
    t = parse_funding(_csv(*ROWS, header=header), MONTH, PUBLISHED)
    rows = t.table.to_pylist()
    assert [r["event_time"] for r in rows] == [
        utc(2026, 10, 1),
        utc(2026, 10, 1, 8, 0, 0, 3000),
        utc(2026, 10, 1, 16),
    ]
    assert {(r["exchange"], r["symbol"], r["ingested_at"]) for r in rows} == {
        ("binance", "AAAUSDT", PUBLISHED)
    }
    # -7.4E-7 is -0.00000074: with the eight decimals of the other rows, mantissa -74.
    assert [(r["funding_rate"], r["funding_rate_scale"]) for r in rows] == [
        (10000, 8),
        (-74, 8),
        (12345, 8),
    ]
    assert [r["interval_hours"] for r in rows] == [8, 8, 4]


def test_an_empty_month_has_no_rows() -> None:
    assert parse_funding(_csv(), MONTH, PUBLISHED).num_rows == 0


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([f"{MS - 1},8,0.0001"], "outside 2026-10"),
        ([f"{MS + 31 * 24 * HOUR},8,0.0001"], "outside 2026-10"),
        ([f"{MS},8,0.0001", f"{MS},8,0.0002"], "repeated or out of order"),
        ([f"{MS},0,0.0001"], "not positive"),
        ([f"{MS},8,high"], "not a plain decimal"),
        ([f"{MS},8"], "CSV"),
        (["soon,8,0.0001"], "CSV"),
    ],
)
def test_bad_files_are_refused(rows: list[str], message: str) -> None:
    with pytest.raises(FundingFileError, match=message):
        parse_funding(_csv(*rows), MONTH, PUBLISHED)
