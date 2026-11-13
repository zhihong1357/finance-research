"""HTTP for data sources: exact bytes back, polite retries, failures as ``SourceError``.

Responses are returned as the bytes the server sent: the raw layer keeps them
exactly. A timeout, a dropped connection, HTTP 429 or a 5xx answer is retried a
few times (``config/collect.toml``, ``[http]``); anything else fails at once.

Tests replace ``TRANSPORT`` with an ``httpx2.MockTransport``, so nothing here
touches the network during tests.
"""

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

import httpx2

from finresearch.core.config import load_toml
from finresearch.sources.base import SourceError

TRANSPORT: httpx2.BaseTransport | None = None  # tests put a mock transport here
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_OK = 200


@cache
def _settings() -> Mapping[str, Any]:
    return load_toml("collect")["http"]


@dataclass(frozen=True, slots=True)
class Reply:
    """A successful response: the body as sent and the headers (names in lower case)."""

    content: bytes
    headers: Mapping[str, str]


def request(
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> bytes:
    """The response body as sent; ``SourceError`` once retries run out or on a hard error."""
    return fetch(method, url, params=params, body=body).content


_local = threading.local()
_pace_lock = threading.Lock()
_next_slot: dict[str, float] = {}


def wait_turn(host: str, per_second: float) -> None:
    """Space requests to ``host`` at most ``per_second`` apart, across all threads.

    Some archives sit behind a firewall that blocks an address sending too many
    requests too fast (Bybit answered 403 after a burst of HEAD requests from
    GitHub Actions). Each request takes the next free slot and waits for it.
    """
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(host, now))
        _next_slot[host] = slot + 1 / per_second
    if slot > now:
        time.sleep(slot - now)


def _client(cfg: Mapping[str, Any]) -> httpx2.Client:
    """One client per thread, reused across requests (keeps connections open).

    A new client is made whenever ``TRANSPORT`` is a different object, so a test
    that swaps in a fake transport never talks through a client made before it.
    The thread keeps a reference to the transport it saw, so its identity cannot
    be reused by a new object while the client lives.
    """
    client: httpx2.Client | None = getattr(_local, "client", None)
    if client is not None and getattr(_local, "transport", None) is TRANSPORT:
        return client
    if client is not None:
        client.close()
    client = httpx2.Client(
        timeout=float(cfg["timeout_seconds"]),
        transport=TRANSPORT,
        headers={"User-Agent": str(cfg["user_agent"])},
    )
    _local.client, _local.transport = client, TRANSPORT
    return client


def fetch(
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Reply:
    """Like ``request``, but also returns the headers (for ETag and Content-Length)."""
    cfg = _settings()
    retries, wait = int(cfg["retries"]), float(cfg["retry_wait_seconds"])
    client = _client(cfg)
    host = httpx2.URL(url).host
    rate = cfg.get("max_per_second", {}).get(host)
    last = ""
    for attempt in range(retries + 1):
        if rate is not None:
            wait_turn(host, float(rate))
        try:
            response = client.request(method, url, params=params, json=body)
        except httpx2.TransportError as exc:
            last = type(exc).__name__
        else:
            if response.status_code == _OK:
                return Reply(response.content, {k.lower(): v for k, v in response.headers.items()})
            last = f"HTTP {response.status_code}"
            if response.status_code not in _RETRY_STATUS:
                break
        if attempt < retries:
            time.sleep(wait)
    raise SourceError(f"{method} {url}: {last}")
