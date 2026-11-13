"""The few GitHub REST calls the monitor needs: issues, workflows, last push.

Inside GitHub Actions the job's own ``GITHUB_TOKEN`` is used, with the
permissions the workflow grants (``issues: write`` and ``actions: write`` for
the monitor). Nothing here is used by research code.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx2

from finresearch.core.time import parse_utc

API_VERSION = "2022-11-28"
_PAGE = 100
_TIMEOUT_SECONDS = 30.0


class GitHubError(RuntimeError):
    """A GitHub call failed."""


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class Workflow:
    id: int
    path: str
    state: str  # "active", "disabled_inactivity", "disabled_manually", ...


class GitHub:
    def __init__(self, repo: str, token: str, *, client: httpx2.Client | None = None) -> None:
        self.repo = repo
        self._client = client or httpx2.Client(
            base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            timeout=_TIMEOUT_SECONDS,
        )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    @classmethod
    def from_environment(cls) -> GitHub:
        repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
        if not repo or not token:
            raise GitHubError("set GITHUB_REPOSITORY and GITHUB_TOKEN (GitHub Actions does)")
        return cls(repo, token)

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ANN401 - JSON
        response = self._client.request(
            method, f"/repos/{self.repo}{path}", headers=self._headers, **kwargs
        )
        if response.status_code >= 400:  # noqa: PLR2004 - HTTP error range
            raise GitHubError(f"{method} {path}: HTTP {response.status_code}")
        return response.json() if response.content else None

    def open_issues(self) -> list[Issue]:
        rows = self._call("GET", "/issues", params={"state": "open", "per_page": _PAGE})
        return [
            Issue(int(r["number"]), str(r["title"]), str(r.get("body") or ""))
            for r in rows
            if "pull_request" not in r
        ]

    def create_issue(self, title: str, body: str) -> int:
        return int(self._call("POST", "/issues", json={"title": title, "body": body})["number"])

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/issues/{number}/comments", json={"body": body})

    def close_issue(self, number: int) -> None:
        self._call("PATCH", f"/issues/{number}", json={"state": "closed"})

    def workflows(self) -> list[Workflow]:
        rows = self._call("GET", "/actions/workflows", params={"per_page": _PAGE})["workflows"]
        return [Workflow(int(r["id"]), str(r["path"]), str(r["state"])) for r in rows]

    def enable_workflow(self, workflow_id: int) -> None:
        self._call("PUT", f"/actions/workflows/{workflow_id}/enable")

    def pushed_at(self) -> datetime:
        return parse_utc(str(self._call("GET", "")["pushed_at"]))
