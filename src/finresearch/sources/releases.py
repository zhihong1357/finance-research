# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""When major U.S. macro numbers came out, to the minute (Phase 2.6.7).

The real-time data set (``philly_rtdsm``) says what was known around the middle
of each month. A study of how markets react to a release needs the instant of
the release itself. Two calendars:

* ``bea.releases``: the Bureau of Economic Analysis's news archive, one page per
  product (GDP; the PCE price index, which comes with Personal Income and
  Outlays), 20 releases a page, back to 1996. Since 2014 each entry carries its
  exact publication time (``datetime="2026-09-30T08:30:00-04:00"``); before
  that most carry only the day, written as midnight Eastern.
* ``bls.releases``: the Bureau of Labor Statistics' list of archived news
  releases for a program (CPI; the Employment Situation, which carries non-farm
  payrolls and the unemployment rate). The release date is in each archived
  file's name (``cpi_11132026.htm``); the time is the BLS's fixed 8:30 a.m.
  Eastern (``config/collect.toml``). BLS turns away automated requests that do
  not name a contact, so these requests carry one; the owner keeps it in the
  GitHub Secret ``BLS_CONTACT`` (never in the repository).

A release is an event. ``timing`` says how well its instant is known:

* ``archive``: the agency's own record gives the minute. ``event_time`` and
  ``available_at`` are that instant.
* ``schedule``: the date is the agency's, the time is its fixed release time
  (BLS). Same two columns.
* ``date_only``: only the day is known. Midnight is not a release time, so the
  row does not pretend: ``event_time`` is the start of that day and
  ``available_at`` its end (both Eastern). Whoever uses the row knows the number
  was out by then, never earlier than it was.

Rows are keyed by ``release`` (``BEA.GDP``, ``BLS.CPI``) and keep the title and
the page they came from. The same release seen again in a later fetch is the
same row.

Terms: BEA and BLS publications are public domain; credit the agency
(docs/architecture.md 7.5).
"""

import html
import os
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

import pyarrow as pa

from finresearch.core.config import load_toml
from finresearch.core.time import ensure_utc, zone
from finresearch.data.table import AVAILABLE_AT, EVENT_TIME, INGESTED_AT, TIME_TYPE, PitTable
from finresearch.sources import http
from finresearch.sources.base import Adapter, Response, SourceError

BEA_SOURCE, BLS_SOURCE = "bea.releases", "bls.releases"
TABLE = "macro.releases"
KEYS = ("release",)
BEA_ARCHIVE = "https://www.bea.gov/news/archive"
BLS_ARCHIVE = "https://www.bls.gov/bls/news-release"
BLS_CONTACT = "BLS_CONTACT"
ARCHIVE, SCHEDULE, DATE_ONLY = "archive", "schedule", "date_only"
_EASTERN = "America/New_York"


class Row(NamedTuple):
    event_time: datetime
    available_at: datetime
    timing: str
    title: str
    page: str


# One row of the archive table: the release's link and title, then its publication time.
_BEA_ENTRY = re.compile(
    r'<tr class="release-row">.*?href="(?P<href>/news/\d{4}/[^"]+)"[^>]*>(?P<title>[^<]+)<'
    r'.*?datetime="(?P<when>[^"]+)"',
    re.S,
)
# BLS keeps placeholder links for coming releases inside HTML comments; they are not releases.
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLS_LINK = re.compile(
    r'href="(?P<href>[^"]*/(?:archives|history)/(?P<code>[a-z]+)_(?P<mmddyyyy>\d{8})\.(?:htm|txt|pdf))"'
)


def bea_fetch(config: Mapping[str, Any], now: datetime) -> list[Response]:
    """Each product's archive, page by page, until a page lists no releases."""
    now = ensure_utc(now)
    out: list[Response] = []
    for name, product in dict(config["products"]).items():
        for page in range(int(config["max_pages"])):
            params = {"field_related_product_target_id": str(product), "page": str(page)}
            content = http.request("GET", BEA_ARCHIVE, params=params)
            query = "&".join(f"{k}={v}" for k, v in params.items())
            out.append(Response(f"{name}-{page}", f"{BEA_ARCHIVE}?{query}", now, content))
            if not _BEA_ENTRY.search(content.decode("utf-8", errors="replace")):
                break
    return out


def bea_parse_url(content: bytes, fetched_at: datetime, url: str) -> PitTable:
    """One archive page -> one row per release listed on it."""
    query = parse_qs(urlsplit(url).query)
    product = query.get("field_related_product_target_id", [""])[0]
    names = {str(v): k for k, v in _bea_products().items()}
    if product not in names:
        raise SourceError(f"{BEA_SOURCE}: unknown product {product!r} in {url}")
    text = _COMMENT.sub("", content.decode("utf-8", errors="replace"))
    rows: list[Row] = []
    for m in _BEA_ENTRY.finditer(text):
        try:
            when = datetime.fromisoformat(m["when"])
        except ValueError as exc:
            raise SourceError(f"{BEA_SOURCE}: {m['when']!r} is not a time") from exc
        title = html.unescape(m["title"]).strip()
        page = "https://www.bea.gov" + m["href"]
        local = when.astimezone(zone(_EASTERN))
        if local.time() == time(0, 0):  # the archive knows only the day
            start = datetime.combine(local.date(), time(0, 0), tzinfo=zone(_EASTERN))
            end = datetime.combine(
                local.date() + timedelta(days=1), time(0, 0), tzinfo=zone(_EASTERN)
            )
            rows.append(Row(ensure_utc(start), ensure_utc(end), DATE_ONLY, title, page))
        else:
            rows.append(Row(ensure_utc(when), ensure_utc(when), ARCHIVE, title, page))
    return _table(f"BEA.{names[product]}", rows, fetched_at)


def _bea_products() -> Mapping[str, Any]:
    return dict(load_toml("collect")["sources"][BEA_SOURCE]["products"])


def bls_fetch(config: Mapping[str, Any], now: datetime) -> list[Response]:
    """Each program's list of archived news releases."""
    contact = os.environ.get(BLS_CONTACT, "").strip()
    if not contact:
        raise SourceError(f"{BLS_SOURCE}: {BLS_CONTACT} is not set (GitHub Secrets)")
    agent = f"{config['agent']} (contact: {contact})"
    now = ensure_utc(now)
    out: list[Response] = []
    for program in config["programs"]:
        url = f"{BLS_ARCHIVE}/{program}.htm"
        content = http.request("GET", url, headers={"User-Agent": agent})
        out.append(Response(str(program), url, now, content))
    return out


def bls_parse_url(content: bytes, fetched_at: datetime, url: str) -> PitTable:
    """One program's archive list -> one row per release date, at the configured time."""
    program = urlsplit(url).path.rsplit("/", 1)[-1].removesuffix(".htm")
    cfg = _bls_config()
    hh, mm = (int(x) for x in str(cfg["release_time"]).split(":"))
    at = time(hh, mm)
    seen: dict[date, str] = {}
    for m in _BLS_LINK.finditer(_COMMENT.sub("", content.decode("utf-8", errors="replace"))):
        if m["code"] != program:
            continue
        d = m["mmddyyyy"]
        try:
            day = date(int(d[4:]), int(d[:2]), int(d[2:4]))
        except ValueError as exc:
            raise SourceError(f"{BLS_SOURCE}: {m['href']!r} names no date") from exc
        href = m["href"] if m["href"].startswith("http") else "https://www.bls.gov" + m["href"]
        seen.setdefault(day, href)
    rows: list[Row] = []
    for day, href in sorted(seen.items()):
        instant = datetime.combine(day, at, tzinfo=zone(str(cfg["release_zone"]))).astimezone(UTC)
        rows.append(Row(instant, instant, SCHEDULE, "", href))
    return _table(f"BLS.{program.upper()}", rows, fetched_at)


def _bls_config() -> Mapping[str, Any]:
    return dict(load_toml("collect")["sources"][BLS_SOURCE])


def _table(release: str, rows: list[Row], fetched_at: datetime) -> PitTable:
    fetched_at = ensure_utc(fetched_at)
    rows = sorted(
        {r.event_time: r for r in rows}.values()
    )  # one row per instant: reissues share it
    if any(r.event_time > fetched_at for r in rows):
        raise SourceError(f"{release}: a release dated after the fetch")
    # A day-only release fetched on that same day: known to be out by now, not by the day's end.
    rows = [r._replace(available_at=min(r.available_at, fetched_at)) for r in rows]
    n = len(rows)
    return PitTable(
        TABLE,
        KEYS,
        pa.table(
            {
                "release": pa.array([release] * n, pa.string()),
                EVENT_TIME: pa.array([r.event_time for r in rows], TIME_TYPE),
                AVAILABLE_AT: pa.array([r.available_at for r in rows], TIME_TYPE),
                INGESTED_AT: pa.array([fetched_at] * n, TIME_TYPE),
                "timing": pa.array([r.timing for r in rows], pa.string()),
                "title": pa.array([r.title for r in rows], pa.string()),
                "page": pa.array([r.page for r in rows], pa.string()),
            }
        ),
    )


BEA_ADAPTER = Adapter(BEA_SOURCE, bea_fetch, parse_url=bea_parse_url)
BLS_ADAPTER = Adapter(BLS_SOURCE, bls_fetch, parse_url=bls_parse_url)
