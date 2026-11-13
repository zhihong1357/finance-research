import json
from collections.abc import Callable

import httpx2
import pytest

from finresearch.core.time import utc
from finresearch.github import GitHub, GitHubError


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.Client:
    transport = httpx2.MockTransport(handler)
    return httpx2.Client(base_url="https://api.github.invalid", transport=transport)


def test_calls_are_scoped_to_the_repository_and_authenticated() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/issues") and request.method == "GET":
            return httpx2.Response(
                200,
                json=[
                    {"number": 1, "title": "outage", "body": "x"},
                    {"number": 2, "title": "a pull request", "body": None, "pull_request": {}},
                ],
            )
        if request.url.path.endswith("/issues") and request.method == "POST":
            return httpx2.Response(201, json={"number": 3})
        if request.url.path == "/repos/o/r":
            return httpx2.Response(200, json={"pushed_at": "2026-11-13T16:04:40Z"})
        if request.url.path.endswith("/actions/workflows"):
            return httpx2.Response(
                200, json={"workflows": [{"id": 5, "path": "p", "state": "active"}]}
            )
        return httpx2.Response(204)

    gh = GitHub("o/r", "secret", client=_client(handler))
    assert [i.number for i in gh.open_issues()] == [1]  # pull requests are not issues
    assert gh.create_issue("t", "b") == 3
    assert json.loads(seen[-1].content) == {"title": "t", "body": "b"}
    assert gh.pushed_at() == utc(2026, 11, 13, 16, 4, 40)
    assert gh.workflows()[0].id == 5
    gh.enable_workflow(5)
    gh.close_issue(1)
    assert (seen[-2].method, seen[-2].url.path) == ("PUT", "/repos/o/r/actions/workflows/5/enable")
    assert json.loads(seen[-1].content) == {"state": "closed"}
    assert all(r.headers["Authorization"] == "Bearer secret" for r in seen)


def test_errors_are_reported() -> None:
    gh = GitHub("o/r", "t", client=_client(lambda _r: httpx2.Response(403)))
    with pytest.raises(GitHubError, match="HTTP 403"):
        gh.open_issues()


def test_credentials_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        GitHub.from_environment()
