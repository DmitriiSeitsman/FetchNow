"""Destination IP classification (no DNS)."""

from __future__ import annotations

import ipaddress
import re
from ipaddress import IPv4Address, IPv6Address

# Hostnames that must never be fetched regardless of provider allowlist.
RESERVED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }
)

_HEX_IPV4_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_DECIMAL_IPV4_RE = re.compile(r"^\d{1,10}$")
_DOTTED_IPV4_RE = re.compile(r"^(\d{1,4}\.){1,3}\d{1,4}$")


def is_reserved_hostname(hostname: str) -> bool:
    """Return True for well-known local/reserved DNS names."""
    return hostname.rstrip(".").lower() in RESERVED_HOSTNAMES


def looks_like_nonstandard_ipv4(hostname: str) -> bool:
    """Detect ambiguous IPv4 encodings that must be rejected fail-closed.

    Standard dotted-decimal with exactly four decimal octets (no leading zeros
    except a lone ``0``) is handled by :func:`parse_ip_literal`. Everything else
    that resembles an alternate IPv4 spelling is rejected without attempting to
    reinterpret it as a hostname.
    """
    host = hostname.rstrip(".").lower()
    if _HEX_IPV4_RE.match(host):
        return True
    if _DECIMAL_IPV4_RE.match(host):
        # Pure integers are dword IPv4 forms (e.g. 2130706433 → 127.0.0.1).
        return True
    if not _DOTTED_IPV4_RE.match(host):
        return False
    parts = host.split(".")
    if len(parts) != 4:
        # Abbreviated forms like 127.1
        return True
    for part in parts:
        if part != "0" and part.startswith("0"):
            # Octal-ish leading zeros (0177.0.0.1)
            return True
        try:
            value = int(part, 10)
        except ValueError:
            return True
        if value > 255:
            return True
    return False


def parse_ip_literal(hostname: str) -> IPv4Address | IPv6Address | None:
    """Parse a hostname that is a standard IP literal, else return None."""
    host = hostname.rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if looks_like_nonstandard_ipv4(host):
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_blocked_destination(address: IPv4Address | IPv6Address) -> bool:
    """Return True when an address must not be contacted.

    Policy is fail-closed. We block non-global addresses and additionally
    treat multicast/reserved/link-local/loopback/private/unspecified as blocked
    even when a given Python version reports ``is_global=True`` for some of them
    (observed for IPv4 multicast on Python 3.12+).
    """
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return is_blocked_destination(address.ipv4_mapped)

    # Explicit cloud metadata endpoint.
    if address == IPv4Address("169.254.169.254"):
        return True

    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        return True

    return not address.is_global


def assert_addresses_allowed(addresses: list[IPv4Address | IPv6Address]) -> None:
    """Fail closed if the set is empty or any address is blocked."""
    from fetchnow.url.errors import raise_validation_error

    if not addresses:
        raise_validation_error("BLOCKED_DESTINATION")
    for address in addresses:
        if is_blocked_destination(address):
            raise_validation_error("BLOCKED_DESTINATION")
