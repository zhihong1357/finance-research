"""HTTP for data sources: exact bytes back, polite retries, failures as ``SourceError``.

Responses are returned as the bytes the server sent: the raw layer keeps them
exactly. A timeout, a dropped connection, HTTP 429 or a 5xx answer is retried a
few times (``config/collect.toml``, ``[http]``); anything else fails at once.

Tests replace ``TRANSPORT`` with an ``httpx2.MockTransport``, so nothing here
touches the network during tests.
"""

import time
from collections.abc import Mapping
from typing import Any

import httpx2

from finresearch.core.config import load_toml
from finresearch.sources.base import SourceError

TRANSPORT: httpx2.BaseTransport | None = None  # tests put a mock transport here
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_OK = 200


def _settings() -> Mapping[str, Any]:
    return load_toml("collect")["http"]


def request(
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> bytes:
    """The response body as sent; ``SourceError`` once retries run out or on a hard error."""
    cfg = _settings()
    retries, wait = int(cfg["retries"]), float(cfg["retry_wait_seconds"])
    headers = {"User-Agent": str(cfg["user_agent"])}
    last = ""
    with httpx2.Client(
        timeout=float(cfg["timeout_seconds"]), transport=TRANSPORT, headers=headers
    ) as client:
        for attempt in range(retries + 1):
            try:
                response = client.request(method, url, params=params, json=body)
            except httpx2.TransportError as exc:
                last = type(exc).__name__
            else:
                if response.status_code == _OK:
                    return response.content
                last = f"HTTP {response.status_code}"
                if response.status_code not in _RETRY_STATUS:
                    break
            if attempt < retries:
                time.sleep(wait)
    raise SourceError(f"{method} {url}: {last}")
