import httpx2
import pytest

from finresearch.sources import http
from finresearch.sources.base import SourceError
from tests.sources.conftest import FakeServer


def test_the_bytes_come_back_exactly(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(200, content=b'{"a": 1.50}')
    assert http.request("GET", "https://x.invalid/p", params={"q": "1"}) == b'{"a": 1.50}'
    (req,) = server.requests
    assert str(req.url) == "https://x.invalid/p?q=1"
    assert req.headers["User-Agent"].startswith("finresearch/")


def test_transient_failures_are_retried(server: FakeServer) -> None:
    answers = iter(
        [httpx2.Response(503), httpx2.Response(429), httpx2.Response(200, content=b"ok")]
    )
    server.handler = lambda _r: next(answers)
    assert http.request("GET", "https://x.invalid/") == b"ok"
    assert len(server.requests) == 3  # retries = 2


def test_retries_run_out(server: FakeServer) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("reset", request=request)

    server.handler = down
    with pytest.raises(SourceError, match="ConnectError"):
        http.request("GET", "https://x.invalid/")
    assert len(server.requests) == 3


def test_a_hard_error_is_not_retried(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(400, content=b'{"error": "bad"}')
    with pytest.raises(SourceError, match="HTTP 400"):
        http.request("POST", "https://x.invalid/", body={"type": "x"})
    assert len(server.requests) == 1


def test_fetch_also_returns_the_headers_in_lower_case(server: FakeServer) -> None:
    server.handler = lambda _r: httpx2.Response(200, content=b"zip", headers={"ETag": '"abc"'})
    reply = http.fetch("GET", "https://x.invalid/f.zip")
    assert reply.content == b"zip"
    assert reply.headers["etag"] == '"abc"'
