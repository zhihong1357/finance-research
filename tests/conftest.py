"""Test-suite guards that enforce the repository's working rules.

* Tests never touch the network (CLAUDE.md): every outbound connection attempt
  raises ``NetworkAccessError``.
* Property-based tests are deterministic: the same code gives the same examples
  on every run, so a CI result can always be reproduced.
"""

import socket

import pytest
from hypothesis import settings

settings.register_profile("deterministic", derandomize=True, deadline=None, print_blob=True)
settings.load_profile("deterministic")


class NetworkAccessError(RuntimeError):
    """Raised when a test tries to open a network connection."""


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise NetworkAccessError("tests must not access the network")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
