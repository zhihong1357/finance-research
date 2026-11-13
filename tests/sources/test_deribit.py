import json

import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.sources.base import SourceError
from finresearch.sources.deribit import ADAPTER, URL, parse
from tests.sources.conftest import FakeServer

NOW = utc(2026, 11, 15, 4, 23)
OPTION = {
    "instrument_name": "BTC-27NOV26-90000-C",
    "bid_price": 0.0125,
    "ask_price": 0.0135,
    "mid_price": 0.013,
    "mark_price": 0.0131,
    "mark_iv": 48.5,
    "underlying_price": 91000.0,
    "underlying_index": "BTC-27NOV26",
    "open_interest": 120.5,
    "volume_usd": 15000.0,
    "interest_rate": 0.0,
    "creation_timestamp": 1794630180000,
    "high": None,
}
PERP = {
    "instrument_name": "SOL_USDC-PERPETUAL",
    "bid_price": 150.1,
    "ask_price": 150.2,
    "mark_price": 150.15,
    "open_interest": 1000.0,
    "current_funding": 0.0001,
    "funding_8h": 0.00008,
    "creation_timestamp": 1794630180000,
}


def _body(rows: list[dict[str, object]]) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "result": rows, "usIn": 1, "usOut": 2}).encode()


def test_one_request_per_currency_and_kind(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(200, content=_body([OPTION]))
    responses = ADAPTER.fetch({"currencies": ["BTC", "USDC"], "kinds": ["future", "option"]}, NOW)
    assert [r.name for r in responses] == ["BTC-future", "BTC-option", "USDC-future", "USDC-option"]
    assert [str(q.url) for q in server.requests][1] == f"{URL}?currency=BTC&kind=option"
    assert {r.fetched_at for r in responses} == {NOW}


def test_parse_keeps_one_row_per_instrument() -> None:
    table = parse(_body([OPTION, PERP]), NOW)
    rows = {r["instrument"]: r for r in table.table.to_pylist()}
    assert rows["BTC-27NOV26-90000-C"]["mark_iv"] == 48.5
    assert rows["BTC-27NOV26-90000-C"]["funding_8h"] is None  # options have no funding
    assert rows["SOL_USDC-PERPETUAL"]["funding_8h"] == 0.00008
    assert {r["available_at"] for r in rows.values()} == {NOW}  # fetch time: no lookahead


def test_an_error_response_is_a_source_error() -> None:
    with pytest.raises(SourceError, match="no result"):
        parse(json.dumps({"jsonrpc": "2.0", "error": {"code": 10001}}).encode(), NOW)
