"""The test harness itself: the guards in conftest.py must actually hold."""

import socket
import urllib.request

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import finresearch
from tests.conftest import NetworkAccessError


def test_package_exposes_its_version() -> None:
    assert finresearch.__version__ == "0.1.0"


def test_raw_socket_connection_is_refused() -> None:
    with socket.socket() as s, pytest.raises(NetworkAccessError):
        s.connect(("192.0.2.1", 80))  # TEST-NET-1: unroutable even if the guard failed


def test_dns_lookup_is_refused() -> None:
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo("example.com", 443)


def test_http_request_is_refused() -> None:
    with pytest.raises(Exception, match="must not access the network"):
        urllib.request.urlopen("http://192.0.2.1/", timeout=1)


def test_property_tests_are_deterministic() -> None:
    assert settings().derandomize is True


@given(st.integers(), st.integers())
def test_property_testing_is_wired_up(a: int, b: int) -> None:
    assert a + b == b + a
