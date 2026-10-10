"""SEC-09 egress dial policy.

Reuses destination classification. Extra infrastructure deny targets come from
configuration — never from a single hard-coded production IP.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Any

from fetchnow.url.destination import is_blocked_destination, parse_ip_literal

GetAddrInfo = Callable[..., Sequence[tuple[Any, ...]]]

_ALLOWED_CONNECT_PORT = 443
_MAX_LABEL = 253


@dataclass(frozen=True, slots=True)
class DialTarget:
    host: str
    port: int
    addresses: tuple[IPv4Address | IPv6Address, ...]


@dataclass(frozen=True, slots=True)
class DenyConfig:
    """Extra always-denied nets/hosts beyond the global blocked-destination set."""

    networks: tuple[IPv4Network | IPv6Network, ...]
    hosts: tuple[IPv4Address | IPv6Address, ...]

    @classmethod
    def from_env(cls) -> DenyConfig:
        nets: list[IPv4Network | IPv6Network] = []
        hosts: list[IPv4Address | IPv6Address] = []
        for raw in _split_csv(os.environ.get("EGRESS_DENY_CIDRS", "")):
            try:
                nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError as exc:
                raise ValueError(f"invalid EGRESS_DENY_CIDRS entry: {raw}") from exc
        for raw in _split_csv(os.environ.get("EGRESS_DENY_IPS", "")):
            try:
                hosts.append(ipaddress.ip_address(raw))
            except ValueError as exc:
                raise ValueError(f"invalid EGRESS_DENY_IPS entry: {raw}") from exc
        return cls(networks=tuple(nets), hosts=tuple(hosts))


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def normalize_connect_host(host: str) -> str:
    text = host.strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text or len(text) > _MAX_LABEL or "\x00" in text or "/" in text:
        raise ValueError("host_invalid")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("host_invalid")
    return text


def is_infrastructure_denied(
    address: IPv4Address | IPv6Address, config: DenyConfig
) -> bool:
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return is_infrastructure_denied(address.ipv4_mapped, config)
    if address in config.hosts:
        return True
    return any(address in network for network in config.networks)


def destination_forbidden(
    address: IPv4Address | IPv6Address, config: DenyConfig
) -> bool:
    return is_blocked_destination(address) or is_infrastructure_denied(address, config)


def resolve_dial_target(
    host: str,
    port: int,
    *,
    config: DenyConfig,
    resolver: GetAddrInfo | None = None,
) -> DialTarget:
    """Resolve and validate every address that may be dialed.

    The returned address tuple is the only set the dialer may use — no
    independent getaddrinfo inside the connect path.
    """
    if port != _ALLOWED_CONNECT_PORT:
        raise ValueError("port_forbidden")
    normalized = normalize_connect_host(host)
    literal = parse_ip_literal(normalized)
    if literal is not None:
        if destination_forbidden(literal, config):
            raise ValueError("destination_forbidden")
        return DialTarget(host=normalized, port=port, addresses=(literal,))

    getaddrinfo: GetAddrInfo = socket.getaddrinfo if resolver is None else resolver
    try:
        infos = getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("dns_failed") from exc
    if not infos:
        raise ValueError("dns_failed")

    ordered: list[IPv4Address | IPv6Address] = []
    seen: set[str] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        ip_text = sockaddr[0]
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError("dns_failed") from exc
        key = str(address)
        if key in seen:
            continue
        seen.add(key)
        if destination_forbidden(address, config):
            raise ValueError("destination_forbidden")
        ordered.append(address)
    if not ordered:
        raise ValueError("dns_failed")
    return DialTarget(host=normalized, port=port, addresses=tuple(ordered))


def dial_tcp(target: DialTarget, *, timeout: float) -> socket.socket:
    """Connect using only pre-validated addresses, in order."""
    last_error: OSError | None = None
    for address in target.addresses:
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect((str(address), target.port))
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise OSError("connect_failed") from last_error
