"""Deribit book summaries: every listed future, perpetual and option, as seen right now.

One request per (currency, kind) to ``public/get_book_summary_by_currency``
returns bid, ask, mark, mark implied volatility, open interest and volume for
every instrument. Deribit keeps no free history of this, so it is a snapshot
source: a summary not recorded is lost.

Terms: market data and data derived from it are for personal use only
(docs/architecture.md 7.5). The raw files live in the private dataset and no
result derived from them goes into the public repository (decision 0008).
"""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from finresearch.core.time import ensure_utc
from finresearch.data.table import PitTable, pit_table
from finresearch.sources import http
from finresearch.sources.base import Adapter, Response, SourceError

SOURCE = "deribit.book_summary"
URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
NUMBERS = {
    "bid": "bid_price",
    "ask": "ask_price",
    "mid": "mid_price",
    "mark": "mark_price",
    "mark_iv": "mark_iv",
    "underlying_price": "underlying_price",
    "open_interest": "open_interest",
    "volume_usd": "volume_usd",
    "interest_rate": "interest_rate",
    "current_funding": "current_funding",
    "funding_8h": "funding_8h",
}


def fetch(config: Mapping[str, Any], now: datetime) -> list[Response]:
    out: list[Response] = []
    for currency in config["currencies"]:
        for kind in config["kinds"]:
            params = {"currency": str(currency), "kind": str(kind)}
            content = http.request("GET", URL, params=params)
            out.append(
                Response(
                    f"{currency}-{kind}",
                    f"{URL}?currency={currency}&kind={kind}",
                    ensure_utc(now),
                    content,
                )
            )
    return out


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def parse(content: bytes, fetched_at: datetime) -> PitTable:
    """One summary -> one row per instrument. Times are the fetch time: no lookahead."""
    payload = json.loads(content)
    if "result" not in payload:
        raise SourceError(f"{SOURCE}: no result in the response: {str(payload.get('error'))[:200]}")
    rows: list[dict[str, Any]] = payload["result"]
    n = len(rows)
    columns: dict[str, list[Any]] = {
        "instrument": [str(r["instrument_name"]) for r in rows],
        "event_time": [fetched_at] * n,
        "available_at": [fetched_at] * n,
        "ingested_at": [fetched_at] * n,
    }
    for column, field in NUMBERS.items():
        columns[column] = [_number(r.get(field)) for r in rows]
    return pit_table(SOURCE, ("instrument",), columns)


ADAPTER = Adapter(SOURCE, fetch, parse)
