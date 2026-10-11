"""Validate EGRESS_DENY_* coverage against caller-supplied required targets.

Production IP/subnets are never hard-coded here. Activation supplies the
required set from a controlled plan/configuration.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network


class EgressDenyCoverageError(ValueError):
    """Deny list missing required infrastructure coverage."""


@dataclass(frozen=True)
class DenySet:
    networks: tuple[IPv4Network | IPv6Network, ...]
    hosts: tuple[IPv4Address | IPv6Address, ...]


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_deny_set(*, cidrs: str, ips: str) -> DenySet:
    nets: list[IPv4Network | IPv6Network] = []
    hosts: list[IPv4Address | IPv6Address] = []
    for raw in _split_csv(cidrs):
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise EgressDenyCoverageError(
                f"invalid EGRESS_DENY_CIDRS entry: {raw}"
            ) from exc
    for raw in _split_csv(ips):
        try:
            hosts.append(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise EgressDenyCoverageError(
                f"invalid EGRESS_DENY_IPS entry: {raw}"
            ) from exc
    return DenySet(networks=tuple(nets), hosts=tuple(hosts))


def _host_covered(address: IPv4Address | IPv6Address, deny: DenySet) -> bool:
    if address in deny.hosts:
        return True
    return any(address in network for network in deny.networks)


def _network_covered(required: IPv4Network | IPv6Network, deny: DenySet) -> bool:
    for network in deny.networks:
        if isinstance(required, IPv4Network) and isinstance(network, IPv4Network):
            if required == network or required.subnet_of(network):
                return True
        elif isinstance(required, IPv6Network) and isinstance(network, IPv6Network):
            if required == network or required.subnet_of(network):
                return True
    # All addresses of a tiny required net covered by host denies is not enough
    # for infrastructure subnets — require network-level coverage.
    return False


def assert_deny_coverage(
    *,
    deny_cidrs: str,
    deny_ips: str,
    required_cidrs: tuple[str, ...],
    required_ips: tuple[str, ...],
) -> DenySet:
    """Fail closed unless every required target is covered.

    Empty required sets are rejected for media-net activation — coverage must
    be proven, not merely a non-empty string.
    """
    if not required_cidrs and not required_ips:
        raise EgressDenyCoverageError(
            "media activation requires a non-empty required deny target set"
        )
    deny = parse_deny_set(cidrs=deny_cidrs, ips=deny_ips)
    if not deny.networks and not deny.hosts:
        raise EgressDenyCoverageError(
            "EGRESS_DENY_CIDRS/EGRESS_DENY_IPS empty; coverage not proven"
        )
    missing: list[str] = []
    for raw in required_cidrs:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError as exc:
            raise EgressDenyCoverageError(f"invalid required CIDR: {raw}") from exc
        if not _network_covered(net, deny):
            missing.append(raw)
    for raw in required_ips:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise EgressDenyCoverageError(f"invalid required IP: {raw}") from exc
        if not _host_covered(addr, deny):
            missing.append(raw)
    if missing:
        raise EgressDenyCoverageError(
            "deny list missing required infrastructure coverage: " + ", ".join(missing)
        )
    return deny
