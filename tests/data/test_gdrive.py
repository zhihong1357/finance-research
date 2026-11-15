import hashlib
import json

import httpx2
import pytest

from finresearch.data import gdrive
from finresearch.data.gdrive import AUTH_URL, FILES_URL, UPLOAD_URL, Drive, DriveError, Settings

S = Settings(timeout_seconds=5, retries=2, retry_wait_seconds=0)
SESSION = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=abc"


class FakeDrive:
    """Drive's API for one folder, in memory."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}  # id -> (name, content)
        self.folders: dict[str, str] = {}  # name -> id
        self.tokens = 0
        self.fail_next: list[int] = []
        self.pending_name = ""
        self.corrupt = False
        self.seen: list[httpx2.Request] = []

    def __call__(self, r: httpx2.Request) -> httpx2.Response:
        self.seen.append(r)
        if self.fail_next:
            return httpx2.Response(self.fail_next.pop(0))
        url = str(r.url).split("?")[0]
        if url == AUTH_URL:
            self.tokens += 1
            return httpx2.Response(200, json={"access_token": f"t{self.tokens}"})
        assert r.headers["Authorization"] == f"Bearer t{self.tokens}"
        if r.method == "GET" and url == FILES_URL:
            q = r.url.params["q"]
            if "mimeType" in q:
                name = q.split("'")[1]
                found = [{"id": self.folders[name], "name": name}] if name in self.folders else []
                return httpx2.Response(200, json={"files": found})
            files = [
                {
                    "id": i,
                    "name": n,
                    "size": str(len(c)),
                    "md5Checksum": hashlib.md5(c, usedforsecurity=False).hexdigest(),
                }
                for i, (n, c) in self.files.items()
            ]
            return httpx2.Response(200, json={"files": files})
        if r.method == "POST" and url == FILES_URL:
            name = json.loads(r.content)["name"]
            self.folders[name] = f"folder-{name}"
            return httpx2.Response(200, json={"id": self.folders[name]})
        if r.method == "POST" and url == UPLOAD_URL:
            self.pending_name = json.loads(r.content)["name"]
            return httpx2.Response(200, headers={"Location": SESSION})
        if r.method == "PUT":
            content = r.content + (b"x" if self.corrupt else b"")
            file_id = f"f{len(self.files)}"
            self.files[file_id] = (self.pending_name, content)
            return httpx2.Response(200, json={"id": file_id})
        if r.method == "GET" and url.startswith(FILES_URL + "/"):
            name, content = self.files[url.rsplit("/", 1)[1]]
            if r.url.params.get("alt") == "media":
                return httpx2.Response(200, content=content)
            md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
            return httpx2.Response(
                200,
                json={
                    "id": url.rsplit("/", 1)[1],
                    "name": name,
                    "size": str(len(content)),
                    "md5Checksum": md5,
                },
            )
        return httpx2.Response(404)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDrive:
    f = FakeDrive()
    monkeypatch.setattr(gdrive, "TRANSPORT", httpx2.MockTransport(f))
    monkeypatch.setattr(gdrive.time, "sleep", lambda _s: None)
    return f


def _drive() -> Drive:
    return Drive("client", "secret", "refresh", S)


def test_folder_upload_list_download(fake: FakeDrive) -> None:
    d = _drive()
    folder = d.folder("finresearch-backup")
    assert d.folder("finresearch-backup") == folder  # found, not created twice
    stored = d.upload(folder, "a__2026-11-14.tar", b"pack")
    assert (stored.name, stored.size) == ("a__2026-11-14.tar", 4)
    assert list(d.list(folder)) == ["a__2026-11-14.tar"]
    assert d.download(stored.id) == b"pack"
    assert fake.tokens == 1
    # The refresh token travels in the form body, never in a URL.
    assert all("refresh" not in str(r.url) for r in fake.seen)


def test_an_upload_stored_differently_is_an_error(fake: FakeDrive) -> None:
    d = _drive()
    fake.corrupt = True
    with pytest.raises(DriveError, match="different bytes"):
        d.upload(d.folder("finresearch-backup"), "a.tar", b"pack")


def test_transient_failures_are_retried_and_an_expired_token_is_renewed(fake: FakeDrive) -> None:
    d = _drive()
    folder = d.folder("finresearch-backup")
    fake.fail_next = [503, 401]
    assert d.list(folder) == {}
    assert fake.tokens == 2  # a new access token after the 401


def test_a_hard_failure_stops_at_once(fake: FakeDrive) -> None:
    d = _drive()
    fake.fail_next = [403]
    with pytest.raises(DriveError, match="HTTP 403"):
        d.folder("finresearch-backup")


def test_names_outside_the_safe_set_are_refused(fake: FakeDrive) -> None:
    with pytest.raises(DriveError, match="unsupported folder name"):
        _drive().folder("x' or name contains '")


def test_missing_credentials_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in gdrive.ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(DriveError, match="GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET"):
        Drive.from_environment(S)
