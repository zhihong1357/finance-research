"""The Hugging Face adapter, against a fake client: tests never touch the network."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.errors import (
    HfHubHTTPError,
    RemoteEntryNotFoundError,
    RepositoryNotFoundError,
)
from huggingface_hub.hf_api import RepoFile, RepoFolder

from finresearch.core.config import load_toml
from finresearch.data.hub import HubRemote
from finresearch.data.store import StoreError, TransientStoreError

HUB = load_toml("storage")["hub"]


def _response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=httpx2.Request("GET", "https://hub.invalid/x"))


class FakeApi:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail: Exception | None = None

    def _call(self, name: str, **kwargs: object) -> None:
        self.calls.append((name, kwargs))
        if self.fail is not None:
            raise self.fail

    def repo_info(self, repo_id: str, **kwargs: object) -> SimpleNamespace:
        self._call("repo_info", repo_id=repo_id, **kwargs)
        return SimpleNamespace(sha="abc123")

    def list_repo_tree(self, repo_id: str, **kwargs: object) -> list[RepoFile | RepoFolder]:
        self._call("list_repo_tree", repo_id=repo_id, **kwargs)
        prefix = str(kwargs["path_in_repo"] or "")
        hits = [p for p in self.files if p.startswith(prefix)]
        if prefix and not hits:
            raise RemoteEntryNotFoundError("no such folder", response=_response(404))
        return [RepoFolder(path=prefix, oid="t")] + [
            RepoFile(path=p, size=len(self.files[p]), oid="b") for p in hits
        ]

    def hf_hub_download(self, repo_id: str, filename: str, **kwargs: object) -> str:
        self._call("hf_hub_download", repo_id=repo_id, filename=filename, **kwargs)
        target = Path(str(kwargs["local_dir"])) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.files[filename])
        return str(target)

    def create_commit(
        self, repo_id: str, operations: list[CommitOperationAdd], **kwargs: object
    ) -> SimpleNamespace:
        self._call("create_commit", repo_id=repo_id, operations=operations, **kwargs)
        for op in operations:
            self.files[op.path_in_repo] = Path(str(op.path_or_fileobj)).read_bytes()
        return SimpleNamespace(oid="def456")

    def delete_folder(self, path_in_repo: str, repo_id: str, **kwargs: object) -> None:
        self._call("delete_folder", path_in_repo=path_in_repo, repo_id=repo_id, **kwargs)
        self.files = {p: b for p, b in self.files.items() if not p.startswith(path_in_repo)}


def _remote(api: FakeApi) -> HubRemote:
    return HubRemote("someone/data", "token", HUB, api=cast("HfApi", api))


def test_the_adapter_maps_each_operation(tmp_path: Path) -> None:
    api = FakeApi({"raw/a/x.json.gz": b"12345", "other/y": b"1"})
    remote = _remote(api)
    assert remote.head() == "abc123"
    assert remote.list("raw/a") == {"raw/a/x.json.gz": 5}  # folders are not files
    assert remote.list("raw/missing") == {}
    assert remote.read("raw/a/x.json.gz") == b"12345"
    (tmp_path / "new").write_bytes(b"new")
    assert remote.commit({"raw/a/n": tmp_path / "new"}, "add", "abc123") == "def456"
    name, kwargs = api.calls[-1]
    assert name == "create_commit"
    assert kwargs["parent_commit"] == "abc123"  # a concurrent writer makes this fail
    assert (kwargs["repo_type"], kwargs["revision"]) == ("dataset", "main")
    operations = cast("list[CommitOperationAdd]", kwargs["operations"])
    assert [op.path_in_repo for op in operations] == ["raw/a/n"]
    remote.remove("raw/a", "clean up")
    assert list(api.files) == ["other/y"]


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (RepositoryNotFoundError("gone", response=_response(404)), StoreError),
        (HfHubHTTPError("bad token", response=_response(401)), StoreError),
        (HfHubHTTPError("forbidden", response=_response(403)), StoreError),
        (HfHubHTTPError("busy", response=_response(429)), TransientStoreError),
        (HfHubHTTPError("oops", response=_response(502)), TransientStoreError),
        (httpx2.ConnectError("reset"), TransientStoreError),
        (TimeoutError("slow"), TransientStoreError),
    ],
)
def test_failures_are_sorted_into_permanent_and_transient(
    error: Exception, kind: type[Exception]
) -> None:
    api = FakeApi({})
    api.fail = error
    with pytest.raises(kind):
        _remote(api).head()


def test_credentials_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HF_DATASET", "someone/data")
    with pytest.raises(StoreError, match="HF_TOKEN"):
        HubRemote.from_environment(HUB)
