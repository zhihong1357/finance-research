# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""CFTC Commitments of Traders: Traders in Financial Futures, futures only (Phase 2.6.5).

Every week the CFTC reports, for each futures market with enough large
traders, the open interest and the positions of dealers, asset managers,
leveraged funds, other reportable traders and small (non-reportable) traders,
as of Tuesday. The financial report covers interest rates, currencies, stock
indices and the CME and other exchanges' bitcoin and ether futures; it starts
in June 2006. Published reports are never revised (CFTC FAQ).

Data comes from the CFTC's public reporting API (``publicreporting.cftc.gov``,
dataset ``gpe5-46if``) as CSV. The historical zip files on cftc.gov sit behind
a bot check, which this code does not try to get past.

Series: ``<contract code>.<field>``, for example ``133741.lev_money_positions_short``
(CME bitcoin). Kept are the levels: open interest, positions, numbers of
traders and concentration. Week-on-week changes and percentages of open
interest are left out: they follow from the levels.

When a report was public (``config/cftc.toml``; owner's choice, 2026-11-15):
15:30 New York time on the third business day after the report date, with
federal holidays and listed closures not counting as business days. This rule
reproduces all 52 releases of the CFTC's 2026 schedule. Reports delayed by a
government shutdown take the date the backlog was cleared instead. If we saw
a report before the rule's time, the time we saw it is used.

``fetch`` also checks that the next report is not late: when the rule says it
should be out and it is not, the fetch fails, so a new shutdown or delay is
noticed and added to the configuration rather than silently mis-dated.

Terms: public domain; credit the CFTC as the source (docs/architecture.md 7.5).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc, to_epoch_us, zone
from finresearch.core.us_calendar import add_business_days
from finresearch.data import csvtext, fixed, vintages
from finresearch.data.csvtext import CsvError
from finresearch.data.fixed import FixedPointError
from finresearch.data.table import PitTable
from finresearch.data.vintages import Frequency, VintageError
from finresearch.sources import http
from finresearch.sources.base import Adapter, Response, SourceError

SOURCE = "cftc.cot_tff"
TABLE = "positions.cftc"
URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.csv"
CODE = "cftc_contract_market_code"
DATE = "report_date_as_yyyy_mm_dd"
FIELDS = (
    "open_interest_all",
    "dealer_positions_long_all",
    "dealer_positions_short_all",
    "dealer_positions_spread_all",
    "asset_mgr_positions_long",
    "asset_mgr_positions_short",
    "asset_mgr_positions_spread",
    "lev_money_positions_long",
    "lev_money_positions_short",
    "lev_money_positions_spread",
    "other_rept_positions_long",
    "other_rept_positions_short",
    "other_rept_positions_spread",
    "tot_rept_positions_long_all",
    "tot_rept_positions_short",
    "nonrept_positions_long_all",
    "nonrept_positions_short_all",
    "traders_tot_all",
    "traders_dealer_long_all",
    "traders_dealer_short_all",
    "traders_dealer_spread_all",
    "traders_asset_mgr_long_all",
    "traders_asset_mgr_short_all",
    "traders_asset_mgr_spread",
    "traders_lev_money_long_all",
    "traders_lev_money_short_all",
    "traders_lev_money_spread",
    "traders_other_rept_long_all",
    "traders_other_rept_short",
    "traders_other_rept_spread",
    "traders_tot_rept_long_all",
    "traders_tot_rept_short_all",
    "conc_gross_le_4_tdr_long",
    "conc_gross_le_4_tdr_short",
    "conc_gross_le_8_tdr_long",
    "conc_gross_le_8_tdr_short",
    "conc_net_le_4_tdr_long_all",
    "conc_net_le_4_tdr_short_all",
    "conc_net_le_8_tdr_long_all",
    "conc_net_le_8_tdr_short_all",
)
COLUMNS = (CODE, DATE, *FIELDS)
_PAGE = 50_000  # rows per request; longer answers come in pages
_WEEK = timedelta(days=7)
_TUESDAY = 1


@dataclass(frozen=True)
class Shutdown:
    first: date
    last: date
    released_by: date


@dataclass(frozen=True)
class Calendar:
    """When reports come out (``config/cftc.toml``)."""

    at: time
    zone: str
    days_after: int
    closures: frozenset[date]
    shutdowns: tuple[Shutdown, ...]
    late_grace: timedelta

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> Calendar:
        hh, mm = (int(x) for x in str(cfg["release_time"]).split(":"))
        return cls(
            time(hh, mm),
            str(cfg["release_zone"]),
            int(cfg["business_days_after"]),
            frozenset(date.fromisoformat(str(d)) for d in cfg["closures"]),
            tuple(
                Shutdown(
                    date.fromisoformat(str(s["first_report"])),
                    date.fromisoformat(str(s["last_report"])),
                    date.fromisoformat(str(s["released_by"])),
                )
                for s in cfg["shutdowns"]
            ),
            timedelta(hours=float(cfg["late_grace_hours"])),
        )

    def _instant(self, day: date) -> datetime:
        return datetime.combine(day, self.at, tzinfo=zone(self.zone)).astimezone(UTC)

    def release(self, report: date) -> datetime:
        """When the report for ``report`` was (or will be) public."""
        for s in self.shutdowns:
            if s.first <= report <= s.last:
                return self._instant(s.released_by)
        return self._instant(add_business_days(report, self.days_after, self.closures))

    def next_report(self, last: date) -> date:
        """The Tuesday of the week after ``last``: the next report date, or a day after it.

        Report dates sometimes move to the Monday (a Tuesday holiday) or elsewhere
        for reasons the calendar does not know; this is only used to tell when the
        next report is late, and the later candidate avoids false alarms.
        """
        start = last + timedelta(days=2)
        return start + timedelta(days=(_TUESDAY - start.weekday()) % 7)


@cache
def calendar() -> Calendar:
    return Calendar.from_config(load_toml("cftc"))


def fetch(config: Mapping[str, Any], now: datetime) -> list[Response]:
    """Recent weeks, or the whole history while the source is new and once a quarter."""
    now = ensure_utc(now)
    added = datetime.fromisoformat(str(config["added"]).replace("Z", "+00:00"))
    full = now - added < timedelta(days=float(config["backfill_days"])) or (
        now.month in {int(m) for m in config["full_history_months"]}
        and now.day <= int(config["full_history_first_days"])
    )
    since = (
        date.fromisoformat(str(config["history_start"]))
        if full
        else now.date() - timedelta(days=int(config["lookback_days"]))
    )
    out: list[Response] = []
    dates: list[date] = []
    offset = 0
    while True:
        params = {
            "$select": ",".join(COLUMNS),
            "$where": f"{DATE} >= '{since.isoformat()}T00:00:00'",
            "$order": "id",  # report date and contract: a stable order for paging
            "$limit": str(_PAGE),
            "$offset": str(offset),
        }
        content = http.request("GET", URL, params=params)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        name = f"tff-{'full' if full else 'recent'}-{offset // _PAGE}"
        out.append(Response(name, f"{URL}?{query}", now, content))
        page = _dates_and_rows(content)
        dates.extend(page[0])
        if page[1] < _PAGE:
            break
        offset += _PAGE
    check_not_late(dates, now, calendar())
    return out


def check_not_late(dates: list[date], now: datetime, cal: Calendar) -> None:
    """Fail when the report after the latest one should be out by now and is not."""
    if not dates:
        raise SourceError(f"{SOURCE}: no report in the requested weeks")
    expected = cal.release(cal.next_report(max(dates)))
    if ensure_utc(now) > expected + cal.late_grace:
        raise SourceError(
            f"{SOURCE}: the report after {max(dates)} was due by {expected.isoformat()}; "
            "if the CFTC announced a delay, add it to config/cftc.toml"
        )


def _read(content: bytes) -> pa.Table:
    try:
        return csvtext.read(content, COLUMNS, (), header=True)
    except CsvError as exc:
        raise SourceError(f"{SOURCE}: {exc}") from exc


def _dates_and_rows(content: bytes) -> tuple[list[date], int]:
    table = _read(content)
    days = {date.fromisoformat(str(t)[:10]) for t in table.column(DATE).to_pylist()}
    return sorted(days), table.num_rows


def parse(content: bytes, fetched_at: datetime) -> PitTable:
    """One response -> every kept field of every contract and week, as versions."""
    fetched_at = ensure_utc(fetched_at)
    table = _read(content)
    cal = calendar()
    days = [date.fromisoformat(str(t)[:10]) for t in table.column(DATE).to_pylist()]
    when: dict[date, int] = {d: to_epoch_us(min(cal.release(d), fetched_at)) for d in set(days)}
    starts = pa.array([to_epoch_us(datetime(d.year, d.month, d.day, tzinfo=UTC)) for d in days])
    known = pa.array([when[d] for d in days], type=pa.int64())
    codes = table.column(CODE).combine_chunks()
    tables: list[PitTable] = []
    for field in FIELDS:
        column = table.column(field).combine_chunks()
        present = pc.not_equal(column, pa.scalar("", pa.string()))
        if not pc.any(present).as_py():
            continue
        try:
            values = fixed.parse(column.filter(present), trim=True)
            series = pa.array(
                [f"{c}.{field}" for c in codes.filter(present).to_pylist()], pa.string()
            )
            tables.append(
                vintages.many(
                    TABLE,
                    series,
                    Frequency.DAY,
                    starts.filter(present),
                    values,
                    known.filter(present),
                    fetched_at,
                )
            )
        except (FixedPointError, VintageError) as exc:
            raise SourceError(f"{SOURCE}: {field}: {exc}") from exc
    return vintages.concat(TABLE, tables)


ADAPTER = Adapter(SOURCE, fetch, parse)
