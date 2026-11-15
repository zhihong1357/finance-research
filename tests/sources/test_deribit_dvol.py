import json

import httpx2
import pytest

from finresearch.core.time import to_epoch_ms, utc
from finresearch.sources.base import SourceError
from finresearch.sources.deribit_dvol import ADAPTER, URL
from tests.sources.conftest import FakeServer

NOW = utc(2026, 11, 15, 1, 53, 30)
T0 = to_epoch_ms(utc(2026, 11, 15, 1, 50))
MINUTE = 60_000
CONFIG = {
    "added": "2026-10-01T00:00:00Z",
    "backfill_days": 2,
    "lookback_days": 2,
    "currencies": ["BTC"],
    "resolutions": {"60": {"backfill_days_back": 200}},
}


def _page(candles: list[list[object]], continuation: int | None = None) -> bytes:
    """A made-up page in the endpoint's format, newest candle first."""
    return json.dumps(
        {"jsonrpc": "2.0", "result": {"data": candles, "continuation": continuation}}
    ).encode()


def _url(res: str = "60", currency: str = "BTC") -> str:
    return f"{URL}?currency={currency}&start_timestamp=0&end_timestamp=1&resolution={res}"


def test_finished_candles_with_their_close_time_as_availability() -> None:
    content = _page(
        [
            [T0 + 3 * MINUTE, 44.1, 44.2, 44.0, 44.1],  # 01:53, still forming at 01:53:30
            [T0 + MINUTE, 44.56, 44.6, 44.5, 44.55],
            [T0, 44.5, 44.57, 44.5, 44.56],
        ]
    )
    t = ADAPTER.parse_record(content, NOW, _url())
    rows = t.table.to_pylist()
    assert (t.name, t.keys) == ("deribit.dvol", ("currency", "resolution"))
    assert [r["event_time"] for r in rows] == [utc(2026, 11, 15, 1, 50), utc(2026, 11, 15, 1, 51)]
    assert [r["available_at"] for r in rows] == [utc(2026, 11, 15, 1, 51), utc(2026, 11, 15, 1, 52)]
    # Prices keep the digits sent, at one scale: 44.5 is 4450 hundredths.
    assert [(r["open"], r["close"], r["scale"]) for r in rows] == [(4450, 4456, 2), (4456, 4455, 2)]
    assert {r["currency"] for r in rows} == {"BTC"}


@pytest.mark.parametrize(
    ("content", "url", "message"),
    [
        (_page([[T0, 1, 1, 1]]), _url(), "a candle without"),
        (_page([[T0 + 30_000, 1, 1, 1, 1]]), _url(), "grid"),
        (_page([[T0, 1, 1, 1, 1], [T0, 1, 1, 1, 1]]), _url(), "same time"),
        (_page([[T0, 1, 1, 1, 1]]), _url("7"), "unknown resolution"),
        (_page([[T0, 1, 1, 1, 1]]), URL, "names no currency"),
        (b'{"error": {"code": 10}}', _url(), "unexpected response shape"),
    ],
)
def test_bad_pages_are_refused(content: bytes, url: str, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        ADAPTER.parse_record(content, NOW, url)


def test_fetch_follows_the_continuation_back_to_the_window_start(server: FakeServer) -> None:
    start = to_epoch_ms(utc(2026, 11, 13, 1, 53, 30))
    pages = {
        str(to_epoch_ms(NOW)): _page([[T0, 1, 1, 1, 1]], continuation=start + MINUTE),
        str(start + MINUTE): _page([[start, 1, 1, 1, 1]], continuation=start - MINUTE),
    }
    server.handler = lambda r: httpx2.Response(200, content=pages[r.url.params["end_timestamp"]])
    responses = ADAPTER.fetch(CONFIG, NOW)
    assert [r.name for r in responses] == ["BTC-60-0", "BTC-60-1"]
    assert {q.url.params["start_timestamp"] for q in server.requests} == {str(start)}
    # While the source is new, the whole window the endpoint still keeps.
    server.requests.clear()
    pages[str(to_epoch_ms(NOW))] = _page([[T0, 1, 1, 1, 1]], continuation=None)
    ADAPTER.fetch({**CONFIG, "added": "2026-11-14T12:00:00Z"}, NOW)
    first = server.requests[0].url.params["start_timestamp"]
    assert first == str(to_epoch_ms(utc(2026, 4, 29, 1, 53, 30)))
