"""Async DNS resolver boundary for URL validation."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Protocol

from fetchnow.url.errors import URLValidationError, raise_validation_error


class DnsResolver(Protocol):
    """Injectable DNS resolver used by the validation pipeline."""

    async def resolve(self, hostname: str) -> list[IPv4Address | IPv6Address]:
        """Return all A/AAAA addresses for hostname or raise URLValidationError."""


@dataclass
class FakeDnsResolver:
    """Deterministic resolver for tests (no network)."""

    records: Mapping[str, Sequence[str]] = field(default_factory=dict)
    timeouts: set[str] = field(default_factory=set)
    empty: set[str] = field(default_factory=set)
    default_addresses: Sequence[str] = ("8.8.8.8",)

    async def resolve(self, hostname: str) -> list[IPv4Address | IPv6Address]:
        host = hostname.lower().rstrip(".")
        if host in self.timeouts:
            raise_validation_error("SOURCE_TIMEOUT")
        if host in self.empty:
            raise_validation_error("BLOCKED_DESTINATION")
        values = self.records.get(host)
        if values is None:
            values = self.default_addresses
        if not values:
            raise_validation_error("BLOCKED_DESTINATION")
        return [ip_address(item) for item in values]


@dataclass(frozen=True, slots=True)
class SystemDnsResolver:
    """Async getaddrinfo-based resolver with timeout."""

    timeout_seconds: float = 3.0

    async def resolve(self, hostname: str) -> list[IPv4Address | IPv6Address]:
        try:
            loop = asyncio.get_running_loop()
            infos = await asyncio.wait_for(
                loop.getaddrinfo(
                    hostname,
                    None,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            raise_validation_error("SOURCE_TIMEOUT")
        except OSError:
            # NXDOMAIN / resolver failures — do not leak details.
            raise_validation_error("BLOCKED_DESTINATION")
        except URLValidationError:
            raise
        except Exception:
            raise_validation_error("INTERNAL_ERROR")

        addresses: list[IPv4Address | IPv6Address] = []
        seen: set[str] = set()
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            raw = sockaddr[0]
            try:
                addr = ip_address(raw)
            except ValueError:
                continue
            key = str(addr)
            if key in seen:
                continue
            seen.add(key)
            addresses.append(addr)

        if not addresses:
            raise_validation_error("BLOCKED_DESTINATION")
        return addresses
