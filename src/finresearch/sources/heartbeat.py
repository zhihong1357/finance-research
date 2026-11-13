"""Heartbeat sources: tiny records that prove the collection pipeline works end to end.

``heartbeat.canary`` writes one small JSON record on every scheduled run. If it
goes stale, the problem is the pipeline (schedule, storage, credentials), not
any market data source.

``heartbeat.drill`` is the same, except that ``simulate_failure = true`` in its
configuration makes every fetch fail. It exists to rehearse an outage: the
monitor must open an issue within the source's ``max_age_hours``, and close it
once the failure is switched off and a record arrives again.
"""

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from finresearch.core.time import ensure_utc, format_utc, parse_utc
from finresearch.data.table import PitTable, pit_table
from finresearch.sources.base import Adapter, Response, SourceError

CANARY, DRILL = "heartbeat.canary", "heartbeat.drill"


def _fetcher(source: str) -> Callable[[Mapping[str, Any], datetime], list[Response]]:
    def fetch(config: Mapping[str, Any], now: datetime) -> list[Response]:
        if config.get("simulate_failure"):
            raise SourceError(
                f"{source}: simulated outage (simulate_failure in config/collect.toml)"
            )
        now = ensure_utc(now)
        body = json.dumps({"source": source, "at": format_utc(now)}, sort_keys=True).encode()
        return [Response("heartbeat", f"heartbeat://{source}", now, body)]

    return fetch


def parse(content: bytes, fetched_at: datetime) -> PitTable:
    payload = json.loads(content)
    at = parse_utc(str(payload["at"]))
    return pit_table(
        str(payload["source"]),
        ("source",),
        {
            "source": [str(payload["source"])],
            "event_time": [at],
            "available_at": [at],
            "ingested_at": [fetched_at],
            "value": [1.0],
        },
    )


ADAPTERS = [Adapter(s, _fetcher(s), parse) for s in (CANARY, DRILL)]
