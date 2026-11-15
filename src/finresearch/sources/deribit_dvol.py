# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Deribit's volatility index DVOL: BTC and ETH, by the minute and the hour (Phase 2.6.6).

DVOL is the 30-day implied volatility Deribit computes from its BTC and ETH
options, in percent a year: crypto's counterpart of the VIX. The public endpoint
``public/get_volatility_index_data`` returns candles (time, open, high, low,
close), newest first, at most 1,000 per request, with a ``continuation`` time
to page further back.

How far back each resolution goes (2026-11-15, docs/architecture.md 7.3):
one-minute candles about six months, hourly and daily ones back to the index's
start in March 2021. So minute candles are a recording: what is not fetched
within about six months is gone. Each fetch takes the last ``lookback_days``
(overlapping, so a missed run loses nothing), and while the source is new it
takes everything still available.

A candle's ``event_time`` is its start; it is public once it closes, so
``available_at`` is its end. The candle still forming when we fetch is left
out. Values keep the digits Deribit sent (fixed-point, ``data/fixed.py``).

Terms: market data and derived data are for personal use only; the raw files
stay in the private dataset (docs/architecture.md 7.5).
"""

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyarrow as pa

from finresearch.core.time import ensure_utc, from_epoch_ms, to_epoch_ms
from finresearch.data import fixed
from finresearch.data.fixed import SCALE_TYPE, FixedPointError
from finresearch.data.table import AVAILABLE_AT, EVENT_TIME, INGESTED_AT, TIME_TYPE, PitTable
from finresearch.sources import http
from finresearch.sources.base import Adapter, Response, SourceError

SOURCE = "deribit.dvol"
TABLE = "deribit.dvol"
URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
KEYS = ("currency", "resolution")
PRICES = ("open", "high", "low", "close")
# Resolutions the endpoint takes, in seconds (``1D`` is a day).
RESOLUTIONS = {"1": 1, "60": 60, "3600": 3600, "43200": 43200, "1D": 86400}
_MS_PER_SECOND = 1000
_CANDLE_FIELDS = 1 + len(PRICES)


def fetch(config: Mapping[str, Any], now: datetime) -> list[Response]:
    """Each currency at each resolution, paged back to the start of the window."""
    now = ensure_utc(now)
    added = datetime.fromisoformat(str(config["added"]).replace("Z", "+00:00"))
    new = now - added < timedelta(days=float(config["backfill_days"]))
    out: list[Response] = []
    for res, spec in dict(config["resolutions"]).items():
        days = float(spec["backfill_days_back"] if new else config["lookback_days"])
        start = to_epoch_ms(now - timedelta(days=days))
        for currency in config["currencies"]:
            end = to_epoch_ms(now)
            page = 0
            while end > start:
                params = {
                    "currency": str(currency),
                    "start_timestamp": str(start),
                    "end_timestamp": str(end),
                    "resolution": str(res),
                }
                content = http.request("GET", URL, params=params)
                query = "&".join(f"{k}={v}" for k, v in params.items())
                name = f"{currency}-{res}-{page}"
                out.append(Response(name, f"{URL}?{query}", now, content))
                continuation = _result(content).get("continuation")
                if not continuation or int(continuation) >= end:
                    break
                end, page = int(continuation), page + 1
    return out


def _result(content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(content, parse_float=str, parse_int=str)
        result: dict[str, Any] = payload["result"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"{SOURCE}: unexpected response shape") from exc
    return result


def parse_url(content: bytes, fetched_at: datetime, url: str) -> PitTable:
    """One page -> one row per finished candle; the currency and resolution come from the URL."""
    fetched_at = ensure_utc(fetched_at)
    query = parse_qs(urlsplit(url).query)
    try:
        currency, res = query["currency"][0], query["resolution"][0]
    except (KeyError, IndexError) as exc:
        raise SourceError(f"{SOURCE}: the URL names no currency or resolution") from exc
    seconds = RESOLUTIONS.get(res)
    if seconds is None:
        raise SourceError(f"{SOURCE}: unknown resolution {res!r}")
    width = seconds * _MS_PER_SECOND
    candles: list[list[str]] = [[str(v) for v in c] for c in _result(content).get("data", [])]
    if any(len(c) != _CANDLE_FIELDS for c in candles):
        raise SourceError(f"{SOURCE}: a candle without time, open, high, low and close")
    now_ms = to_epoch_ms(fetched_at)
    done = sorted((c for c in candles if int(c[0]) + width <= now_ms), key=lambda c: int(c[0]))
    starts = [int(c[0]) for c in done]
    if len(set(starts)) != len(starts):
        raise SourceError(f"{SOURCE}: two candles start at the same time")
    if any(t % width for t in starts):
        raise SourceError(f"{SOURCE}: a candle does not start on its {res}-second grid")
    n = len(done)
    try:
        prices = fixed.parse([c[i] for i in range(1, _CANDLE_FIELDS) for c in done], trim=True)
    except FixedPointError as exc:
        raise SourceError(f"{SOURCE}: {exc}") from exc
    columns: dict[str, Any] = {
        "currency": pa.array([currency] * n, pa.string()),
        "resolution": pa.array([res] * n, pa.string()),
        EVENT_TIME: pa.array([from_epoch_ms(t) for t in starts], TIME_TYPE),
        AVAILABLE_AT: pa.array([from_epoch_ms(t + width) for t in starts], TIME_TYPE),
        INGESTED_AT: pa.array([fetched_at] * n, TIME_TYPE),
    }
    for i, name in enumerate(PRICES):
        columns[name] = prices.mantissa.slice(i * n, n)
    columns["scale"] = pa.array([prices.scale] * n, SCALE_TYPE)
    return PitTable(TABLE, KEYS, pa.table(columns))


ADAPTER = Adapter(SOURCE, fetch, parse_url=parse_url)
