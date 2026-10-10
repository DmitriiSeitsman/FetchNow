"""Offline regressions for SEC-09 egress dial policy and CONNECT proxy."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest

from fetchnow.media_egress_proxy.policy import (
    DenyConfig,
    destination_forbidden,
    resolve_dial_target,
)
from fetchnow.media_egress_proxy.server import (
    EgressProxy,
    ProxyLimits,
    _split_authority,
)


def test_private_and_metadata_forbidden() -> None:
    cfg = DenyConfig(networks=(), hosts=())
    assert destination_forbidden(ipaddress.ip_address("10.0.0.1"), cfg)
    assert destination_forbidden(ipaddress.ip_address("127.0.0.1"), cfg)
    assert destination_forbidden(ipaddress.ip_address("169.254.169.254"), cfg)
    assert destination_forbidden(ipaddress.ip_address("::1"), cfg)
    assert destination_forbidden(ipaddress.ip_address("::ffff:127.0.0.1"), cfg)


def test_infrastructure_deny_cidrs() -> None:
    cfg = DenyConfig(
        networks=(ipaddress.ip_network("203.0.113.0/24"),),
        hosts=(ipaddress.ip_address("198.51.100.10"),),
    )
    assert destination_forbidden(ipaddress.ip_address("203.0.113.5"), cfg)
    assert destination_forbidden(ipaddress.ip_address("198.51.100.10"), cfg)


def test_resolve_rejects_forbidden_any_address() -> None:
    def fake_getaddrinfo(host: str, port: int, type: int = 0) -> list[Any]:
        del host, port, type
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.1.2.3", 443),
            ),
        ]

    with pytest.raises(ValueError, match="destination_forbidden"):
        resolve_dial_target(
            "example.test",
            443,
            config=DenyConfig(networks=(), hosts=()),
            resolver=fake_getaddrinfo,
        )


def test_resolve_accepts_public_only_set() -> None:
    def fake_getaddrinfo(host: str, port: int, type: int = 0) -> list[Any]:
        del host, port, type
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:4700:4700::1111", 443, 0, 0),
            ),
        ]

    target = resolve_dial_target(
        "example.test",
        443,
        config=DenyConfig(networks=(), hosts=()),
        resolver=fake_getaddrinfo,
    )
    assert [str(a) for a in target.addresses] == [
        "1.1.1.1",
        "2606:4700:4700::1111",
    ]


def test_connect_port_must_be_443() -> None:
    with pytest.raises(ValueError, match="port_forbidden"):
        resolve_dial_target(
            "1.1.1.1",
            80,
            config=DenyConfig(networks=(), hosts=()),
        )


def test_authority_parser_rejects_urls() -> None:
    with pytest.raises(ValueError):
        _split_authority("https://evil.test:443")
    with pytest.raises(ValueError):
        _split_authority("evil.test:443/path")
    host, port = _split_authority("evil.test:443")
    assert host == "evil.test" and port == 443


def test_proxy_rejects_non_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = EgressProxy(
        deny=DenyConfig(networks=(), hosts=()),
        limits=ProxyLimits(max_connections=2),
    )
    sent: list[bytes] = []

    class FakeSock:
        def settimeout(self, _value: float | None) -> None:
            return None

        def recv(self, _n: int) -> bytes:
            return b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n"

        def sendall(self, data: bytes) -> None:
            sent.append(data)

        def close(self) -> None:
            return None

    proxy.handle_client(FakeSock())  # type: ignore[arg-type]
    assert sent and sent[0].startswith(b"HTTP/1.1 405")
