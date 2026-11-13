"""Fake HTTP for source tests. Payloads are written by hand in each source's format,
with made-up numbers: no third-party data goes into the repository."""

from collections.abc import Callable

import httpx2
import pytest

from finresearch.sources import http

Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeServer:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.handler: Handler = lambda _r: httpx2.Response(404)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)


def install(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    """Route every request of ``finresearch.sources.http`` to a new fake server."""
    fake = FakeServer()
    monkeypatch.setattr(http, "TRANSPORT", httpx2.MockTransport(fake))
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    return fake


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    return install(monkeypatch)
