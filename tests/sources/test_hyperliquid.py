import json

import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.sources.base import SourceError
from finresearch.sources.hyperliquid import ADAPTER, REQUEST, parse
from tests.sources.conftest import FakeServer

NOW = utc(2026, 11, 15, 4, 23)
META = {"universe": [{"name": "AAA", "szDecimals": 2}, {"name": "BBB", "szDecimals": 1}]}
CTXS = [
    {
        "funding": "0.0000125",
        "openInterest": "1500.5",
        "markPx": "10.5",
        "oraclePx": "10.49",
        "midPx": "10.5",
        "premium": "0.0001",
        "dayNtlVlm": "250000.0",
        "prevDayPx": "10.1",
        "impactPxs": ["10.49", "10.51"],
    },
    {
        "funding": "-0.00002",
        "openInterest": "20.0",
        "markPx": "3.2",
        "oraclePx": "3.21",
        "midPx": None,
        "premium": "-0.0002",
        "dayNtlVlm": "0.0",
        "prevDayPx": "3.3",
        "impactPxs": None,
    },
]


def test_one_post_with_the_documented_body(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(200, content=json.dumps([META, CTXS]).encode())
    (r,) = ADAPTER.fetch({}, NOW)
    (req,) = server.requests
    assert req.method == "POST"
    assert json.loads(req.content) == REQUEST
    assert r.fetched_at == NOW


def test_parse_pairs_assets_with_contexts() -> None:
    table = parse(json.dumps([META, CTXS]).encode(), NOW)
    rows = {r["coin"]: r for r in table.table.to_pylist()}
    assert rows["AAA"]["funding"] == pytest.approx(0.0000125)
    assert rows["AAA"]["open_interest"] == pytest.approx(1500.5)
    assert rows["BBB"]["mid"] is None


def test_a_mismatched_response_is_refused() -> None:
    with pytest.raises(SourceError, match="2 assets but 1 contexts"):
        parse(json.dumps([META, CTXS[:1]]).encode(), NOW)
    with pytest.raises(SourceError, match="unexpected response shape"):
        parse(json.dumps({"error": "x"}).encode(), NOW)
