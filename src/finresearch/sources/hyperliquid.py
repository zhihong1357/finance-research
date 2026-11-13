"""Hyperliquid perpetuals: funding, open interest and prices of every listed asset.

One request, ``{"type": "metaAndAssetCtxs"}`` to the public ``info`` endpoint,
returns the list of perpetuals and, for each, the current funding rate, open
interest, mark, oracle and mid prices, premium and daily volume. Funding history
can be fetched again later; open interest and the rest cannot, so this is a
snapshot source.
"""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from finresearch.core.time import ensure_utc
from finresearch.data.table import PitTable, pit_table
from finresearch.sources import http
from finresearch.sources.base import Adapter, Response, SourceError

SOURCE = "hyperliquid.asset_ctxs"
URL = "https://api.hyperliquid.xyz/info"
REQUEST = {"type": "metaAndAssetCtxs"}
NUMBERS = {
    "funding": "funding",
    "open_interest": "openInterest",
    "mark": "markPx",
    "oracle": "oraclePx",
    "mid": "midPx",
    "premium": "premium",
    "day_notional_volume": "dayNtlVlm",
    "prev_day_price": "prevDayPx",
}


def fetch(_config: Mapping[str, Any], now: datetime) -> list[Response]:
    content = http.request("POST", URL, body=REQUEST)
    return [Response("metaAndAssetCtxs", URL, ensure_utc(now), content)]


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def parse(content: bytes, fetched_at: datetime) -> PitTable:
    """One snapshot -> one row per perpetual (the n-th context belongs to the n-th asset)."""
    payload = json.loads(content)
    try:
        meta, contexts = payload
        universe: list[dict[str, Any]] = meta["universe"]
    except (TypeError, ValueError, KeyError) as exc:
        raise SourceError(f"{SOURCE}: unexpected response shape") from exc
    if len(universe) != len(contexts):
        raise SourceError(f"{SOURCE}: {len(universe)} assets but {len(contexts)} contexts")
    n = len(universe)
    columns: dict[str, list[Any]] = {
        "coin": [str(a["name"]) for a in universe],
        "event_time": [fetched_at] * n,
        "available_at": [fetched_at] * n,
        "ingested_at": [fetched_at] * n,
    }
    for column, field in NUMBERS.items():
        columns[column] = [_number(c.get(field)) for c in contexts]
    return pit_table(SOURCE, ("coin",), columns)


ADAPTER = Adapter(SOURCE, fetch, parse)
