"""The Philadelphia Fed's Real-Time Data Set for Macroeconomists (Phase 2.6.4).

For each macro variable the data set has one workbook in which every column is
a *vintage*: the variable's whole history as the public could see it at that
time (docs/architecture.md 7.3). Rows are observation periods (``1947:Q1``,
``1947:01``); a cell is ``#N/A`` where the vintage has no value yet. Column
headers name the vintage: ``ROUTPUT65M11`` is the November 1965 monthly
vintage, ``RUC65Q4`` the fourth-quarter 1965 vintage.

What a vintage date means, from the data set's documentation: a monthly vintage
is the data available "around the middle" of its month; a quarterly vintage the
data available in the middle month of its quarter (February, May, August or
November). No day is given. So a vintage's ``available_at`` is the first day of
the month after (after the middle month, for a quarterly vintage): never early,
at most about two weeks late. The latest vintages can be newer than that bound
when we fetch them (the data set is updated as the agencies publish), so
``available_at`` is the earlier of the bound and the fetch time: we saw the
values then, so they were public then.

Two-digit vintage years take the century that puts them no later than the
fetch: ``64`` is 1964 and ``26`` is 2026 when fetched in 2026.

Levels are as published. Index levels (prices, hours) change base period from
one vintage to another, so compare growth rates within one vintage, not levels
across vintages.

The links to the workbooks carry a hash that changes, so ``fetch`` reads each
variable's page and takes the link to the configured file from it.

Terms: research use is allowed; the files stay in the private dataset
(docs/architecture.md 7.5).
"""

import html
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from finresearch.core.time import ensure_utc, month_start
from finresearch.data import fixed, sheets, vintages
from finresearch.data.fixed import FixedPointError
from finresearch.data.sheets import SheetError
from finresearch.data.table import PitTable
from finresearch.data.vintages import Frequency, VintageError
from finresearch.sources import http
from finresearch.sources.base import Adapter, Response, SourceError

SOURCE = "philly.rtdsm"
TABLE = "macro.rtdsm"
SITE = "https://www.philadelphiafed.org"
PAGE = f"{SITE}/surveys-and-data/real-time-data-research"
MISSING = "#N/A"
_HEADER = re.compile(r"^(?P<series>[A-Z]+?)(?P<yy>\d{2})(?P<kind>[MQ])(?P<n>\d{1,2})$")
_PERIOD_Q = re.compile(r"^(?P<y>\d{4}):Q(?P<q>[1-4])$")
_PERIOD_M = re.compile(r"^(?P<y>\d{4}):(?P<m>\d{2})$")
_MONTHS_PER_QUARTER = 3
_CENTURY = 100
_MONTHS = 12


def fetch(config: Mapping[str, Any], now: datetime) -> list[Response]:
    """Each configured workbook, found through its variable's page."""
    now = ensure_utc(now)
    out: list[Response] = []
    for page, file in dict(config["files"]).items():
        listing = http.request("GET", f"{PAGE}/{page}").decode("utf-8", errors="replace")
        links = re.findall(rf'href="(/-/media/[^"]*/{re.escape(str(file))}[^"]*)"', listing)
        if len(set(links)) != 1:
            raise SourceError(f"{SOURCE}: {len(set(links))} links to {file} on the {page} page")
        url = SITE + html.unescape(links[0])
        out.append(Response(str(file).removesuffix(".xlsx"), url, now, http.request("GET", url)))
    return out


def vintage_year(yy: int, fetched_at: datetime) -> int:
    year = fetched_at.year // _CENTURY * _CENTURY + yy
    return year - _CENTURY if year > fetched_at.year else year


def known_by(header: str, fetched_at: datetime) -> tuple[str, datetime]:
    """The series and the ``available_at`` of a vintage column such as ``ROUTPUT65M11``."""
    m = _HEADER.match(header)
    if m is None:
        raise SourceError(f"{SOURCE}: {header!r} is not a vintage column")
    year, n = vintage_year(int(m["yy"]), fetched_at), int(m["n"])
    month = n if m["kind"] == "M" else n * _MONTHS_PER_QUARTER - 1  # the quarter's middle month
    if not 1 <= month <= _MONTHS:
        raise SourceError(f"{SOURCE}: {header!r} names no month")
    after = month_start(date(year, month, 1), 1)
    bound = datetime(after.year, after.month, after.day, tzinfo=UTC)
    return m["series"], min(bound, fetched_at)


def period(label: object) -> tuple[date, Frequency]:
    text = str(label).strip()
    if m := _PERIOD_Q.match(text):
        return date(int(m["y"]), (int(m["q"]) - 1) * _MONTHS_PER_QUARTER + 1, 1), Frequency.QUARTER
    if m := _PERIOD_M.match(text):
        return date(int(m["y"]), int(m["m"]), 1), Frequency.MONTH
    raise SourceError(f"{SOURCE}: {text!r} is not an observation period")


def parse(content: bytes, fetched_at: datetime) -> PitTable:
    """One workbook -> every value of every vintage, repeats within it dropped."""
    try:
        cells = sheets.rows(content)
    except SheetError as exc:
        raise SourceError(f"{SOURCE}: {exc}") from exc
    return parse_rows(cells, fetched_at)


def parse_rows(cells: list[list[sheets.Cell]], fetched_at: datetime) -> PitTable:
    """The cells of a workbook's sheet (``DATE`` column, then one column per vintage)."""
    fetched_at = ensure_utc(fetched_at)
    if not cells or not cells[0] or str(cells[0][0]).strip().upper() != "DATE":
        raise SourceError(f"{SOURCE}: the first column is not DATE")
    headers = [str(h).strip() for h in cells[0][1:]]
    periods = [period(r[0]) for r in cells[1:]]
    if len({f for _, f in periods}) > 1:
        raise SourceError(f"{SOURCE}: monthly and quarterly periods in one sheet")
    tables: list[PitTable] = []
    for col, header in enumerate(headers, start=1):
        series, known = known_by(header, fetched_at)
        starts: list[date] = []
        texts: list[str] = []
        for (start, _), row in zip(periods, cells[1:], strict=True):
            cell = row[col] if col < len(row) else None
            if cell is None or cell == "" or cell == MISSING:
                continue
            if not isinstance(cell, float | int) or isinstance(cell, bool):
                raise SourceError(f"{SOURCE}: {header} {start}: {cell!r} is not a number")
            starts.append(start)
            texts.append(sheets.number_text(cell))
        if not starts:
            continue
        try:
            values = fixed.parse(texts, floats=True, trim=True)
            tables.append(
                vintages.versions(
                    TABLE, series, periods[0][1], starts, values, [known] * len(starts), fetched_at
                )
            )
        except (FixedPointError, VintageError) as exc:
            raise SourceError(f"{SOURCE}: {header}: {exc}") from exc
    if len({t.table.column("series")[0].as_py() for t in tables}) > 1:
        raise SourceError(f"{SOURCE}: more than one variable in one workbook")
    return vintages.compact(vintages.concat(TABLE, tables))


ADAPTER = Adapter(SOURCE, fetch, parse)
