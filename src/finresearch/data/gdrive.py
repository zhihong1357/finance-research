"""Google Drive as the offsite backup (Phase 2.9, decision 0014).

A small client for the Drive REST API (v3), enough to keep backup files in one
folder: find or create the folder, list it, upload a file, download a file.

Authorization is an OAuth refresh token for the ``drive.file`` scope: the app
sees only the files it created itself, nothing else in the owner's Drive. The
client ID, client secret and refresh token come from the environment
(``GDRIVE_CLIENT_ID``, ``GDRIVE_CLIENT_SECRET``, ``GDRIVE_REFRESH_TOKEN``,
which GitHub Actions fills from Secrets); they are never written anywhere.

Uploads use Drive's resumable protocol (one session, then the bytes in one
request), which has no size limit for our files, and every upload is checked
against the MD5 that Drive reports for the stored file. Transient failures
(timeouts, dropped connections, HTTP 429 and 5xx) are retried.

Tests replace ``TRANSPORT`` with an ``httpx2.MockTransport``.
"""

import hashlib
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx2

TRANSPORT: httpx2.BaseTransport | None = None  # tests put a mock transport here
AUTH_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_TYPE = "application/vnd.google-apps.folder"
ENV = ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN")
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_OK = frozenset({200, 201})
_UNAUTHORIZED = 401
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")  # names we create: safe inside a Drive query


class DriveError(RuntimeError):
    """Drive refused a request, or a file came back different from what was sent."""


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    size: int
    md5: str


@dataclass(frozen=True)
class Settings:
    timeout_seconds: float
    retries: int
    retry_wait_seconds: float

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> Settings:
        return cls(
            float(cfg["timeout_seconds"]), int(cfg["retries"]), float(cfg["retry_wait_seconds"])
        )


class Drive:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, s: Settings) -> None:
        self._credentials = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        self._s = s
        self._http = httpx2.Client(timeout=s.timeout_seconds, transport=TRANSPORT)
        self._token = ""

    @classmethod
    def from_environment(cls, s: Settings) -> Drive:
        missing = [name for name in ENV if not os.environ.get(name)]
        if missing:
            raise DriveError(f"not set: {', '.join(missing)} (GitHub Secrets)")
        client_id, client_secret, refresh_token = (os.environ[name] for name in ENV)
        return cls(client_id, client_secret, refresh_token, s)

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        form: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx2.Response:
        """One request with retries; a fresh access token when the old one has expired."""
        authorized = url != AUTH_URL
        last = ""
        for attempt in range(self._s.retries + 1):
            if authorized and not self._token:
                self._refresh()
            sent = dict(headers or {})
            if authorized:
                sent["Authorization"] = f"Bearer {self._token}"
            try:
                r = self._http.request(
                    method, url, headers=sent, params=params, json=body, data=form, content=content
                )
            except httpx2.TransportError as exc:
                last = type(exc).__name__
            else:
                if r.status_code in _OK:
                    return r
                last = f"HTTP {r.status_code}"
                if authorized and r.status_code == _UNAUTHORIZED:
                    self._token = ""  # expired: the next attempt gets a new one
                elif r.status_code not in _RETRY_STATUS:
                    break
            if attempt < self._s.retries:
                time.sleep(self._s.retry_wait_seconds)
        # The URL never carries a secret: tokens travel in headers and form bodies only.
        raise DriveError(f"{method} {url.split('?')[0]}: {last}")

    def _refresh(self) -> None:
        r = self._send("POST", AUTH_URL, form=self._credentials)
        token = r.json().get("access_token")
        if not token:
            raise DriveError("the token endpoint returned no access token")
        self._token = str(token)

    def _query(self, q: str) -> list[DriveFile]:
        out: list[DriveFile] = []
        page = ""
        while True:
            params = {
                "q": q,
                "fields": "nextPageToken,files(id,name,size,md5Checksum)",
                "pageSize": "1000",
                "spaces": "drive",
            }
            if page:
                params["pageToken"] = page
            body = self._send("GET", FILES_URL, params=params).json()
            out.extend(
                DriveFile(
                    str(f["id"]),
                    str(f["name"]),
                    int(f.get("size", 0)),
                    str(f.get("md5Checksum", "")),
                )
                for f in body.get("files", [])
            )
            page = str(body.get("nextPageToken", ""))
            if not page:
                return out

    def folder(self, name: str) -> str:
        """The id of the app's folder called ``name``, created if it does not exist yet."""
        if not _NAME.match(name):
            raise DriveError(f"unsupported folder name {name!r}")
        q = f"name = '{name}' and mimeType = '{FOLDER_TYPE}' and trashed = false"
        found = self._query(q)
        if len(found) > 1:
            raise DriveError(f"{len(found)} folders named {name!r}; expected one")
        if found:
            return found[0].id
        meta = {"name": name, "mimeType": FOLDER_TYPE}
        return str(self._send("POST", FILES_URL, body=meta, params={"fields": "id"}).json()["id"])

    def list(self, folder_id: str) -> dict[str, DriveFile]:
        """The files in a folder, by name."""
        files = self._query(f"'{folder_id}' in parents and trashed = false")
        by_name: dict[str, DriveFile] = {}
        for f in files:
            if f.name in by_name:
                raise DriveError(f"two files named {f.name!r} in the backup folder")
            by_name[f.name] = f
        return by_name

    def upload(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        """Upload one file; checked against the MD5 Drive reports for what it stored."""
        meta = {"name": name, "parents": [folder_id]}
        session = self._send(
            "POST",
            UPLOAD_URL,
            params={"uploadType": "resumable"},
            body=meta,
            headers={"X-Upload-Content-Type": "application/octet-stream"},
        )
        location = session.headers.get("location")
        if not location:
            raise DriveError("Drive opened no upload session")
        put = self._send(
            "PUT", location, content=content, headers={"Content-Type": "application/octet-stream"}
        )
        file_id = str(put.json()["id"])
        f = self._send(
            "GET", f"{FILES_URL}/{file_id}", params={"fields": "id,name,size,md5Checksum"}
        ).json()
        stored = DriveFile(str(f["id"]), str(f["name"]), int(f["size"]), str(f["md5Checksum"]))
        if stored.md5 != hashlib.md5(content, usedforsecurity=False).hexdigest():
            raise DriveError(f"{name}: Drive stored different bytes than were sent")
        return stored

    def download(self, file_id: str) -> bytes:
        return self._send("GET", f"{FILES_URL}/{file_id}", params={"alt": "media"}).content

    def close(self) -> None:
        self._http.close()
