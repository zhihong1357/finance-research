"""The Hugging Face side of the remote store (decision 0003).

``HubRemote`` adapts the ``huggingface_hub`` client to ``store.Remote``. It
sorts failures into two kinds: a wrong token, a missing repository or a missing
revision is permanent (``StoreError``); everything else that comes back from
the network is worth retrying (``TransientStoreError``).

The repository id and the token come from the environment (``HF_DATASET`` and
``HF_TOKEN``), never from the repository: the id contains an account name.
"""

import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from httpx2 import HTTPError
from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    RemoteEntryNotFoundError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)
from huggingface_hub.hf_api import RepoFile

from finresearch.data.store import StoreError, TransientStoreError

TOKEN_ENV, DATASET_ENV = "HF_TOKEN", "HF_DATASET"
_PERMANENT_STATUS = frozenset({401, 403, 404})


def _classify[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except (RepositoryNotFoundError, RevisionNotFoundError, GatedRepoError) as exc:
        raise StoreError(f"{type(exc).__name__}: check HF_DATASET and the token") from exc
    except HfHubHTTPError as exc:
        status = exc.response.status_code
        if status in _PERMANENT_STATUS:
            raise StoreError(f"HTTP {status}: check HF_DATASET and the token") from exc
        raise TransientStoreError(f"HTTP {status}") from exc
    except (HTTPError, OSError) as exc:
        raise TransientStoreError(type(exc).__name__) from exc


class HubRemote:
    def __init__(
        self,
        repo_id: str,
        token: str,
        hub: Mapping[str, Any],
        api: HfApi | None = None,
    ) -> None:
        self._repo = repo_id
        self._type = str(hub["repo_type"])
        self._revision = str(hub["revision"])
        self._api = api or HfApi(token=token)

    @classmethod
    def from_environment(cls, hub: Mapping[str, Any]) -> HubRemote:
        token, repo = os.environ.get(TOKEN_ENV), os.environ.get(DATASET_ENV)
        if not token or not repo:
            raise StoreError(
                f"set {TOKEN_ENV} (a GitHub Secret) and {DATASET_ENV} (a GitHub Variable)"
            )
        return cls(repo, token, hub)

    def head(self) -> str:
        def sha() -> str:
            info = self._api.repo_info(self._repo, repo_type=self._type, revision=self._revision)
            if info.sha is None:
                raise TransientStoreError("the hub did not report the head commit")
            return info.sha

        return _classify(sha)

    def list(self, prefix: str) -> dict[str, int]:
        def files() -> dict[str, int]:
            try:
                tree = self._api.list_repo_tree(
                    self._repo,
                    path_in_repo=prefix or None,
                    recursive=True,
                    repo_type=self._type,
                    revision=self._revision,
                )
                return {e.path: e.size for e in tree if isinstance(e, RepoFile)}
            except RemoteEntryNotFoundError:
                return {}

        return _classify(files)

    def read(self, path: str) -> bytes:
        def download() -> bytes:
            with tempfile.TemporaryDirectory(prefix="finresearch-hub-") as tmp:
                local = self._api.hf_hub_download(  # pyright: ignore[reportUnknownMemberType]
                    self._repo,
                    path,
                    repo_type=self._type,
                    revision=self._revision,
                    local_dir=tmp,
                )
                return Path(str(local)).read_bytes()

        return _classify(download)

    def commit(self, files: Mapping[str, Path], message: str, parent: str) -> str:
        operations = [CommitOperationAdd(path, str(source)) for path, source in files.items()]

        def create() -> str:
            info = self._api.create_commit(
                self._repo,
                operations,
                commit_message=message,
                repo_type=self._type,
                revision=self._revision,
                parent_commit=parent,
            )
            return info.oid

        return _classify(create)

    def remove(self, prefix: str, message: str) -> None:
        def delete() -> None:
            self._api.delete_folder(
                prefix,
                self._repo,
                repo_type=self._type,
                revision=self._revision,
                commit_message=message,
            )

        _classify(delete)
