"""Immutable exact-host wrapper resolver registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from fetchnow.network.models import SafeDocumentTarget
from fetchnow.resolution.protocols import WrapperResolver

# Module-private construction seal — not part of the public API.
_REGISTRY_SEAL = object()


class WrapperRegistryError(ValueError):
    """Invalid or ambiguous wrapper registry configuration."""


def _normalize_host(hostname: str) -> str:
    host = hostname.strip().lower().rstrip(".")
    if not host:
        raise WrapperRegistryError("wrapper hostname must be non-empty")
    if "/" in host or "\\" in host or ".." in host or ":" in host:
        raise WrapperRegistryError(f"unsafe wrapper hostname: {hostname!r}")
    if "%" in host:
        raise WrapperRegistryError(
            f"percent-encoded wrapper hostname rejected: {hostname!r}"
        )
    return host


@dataclass(frozen=True, slots=True)
class WrapperResolverRegistration:
    """Immutable registration snapshot captured at registry build time.

    Host ownership and audit identifiers are frozen here. Runtime code must not
    re-read ``resolver.exact_hostnames`` / live ID properties for security policy.
    """

    resolver: WrapperResolver = field(repr=False)
    resolver_id: str
    wrapper_type: str
    exact_hostnames: frozenset[str]


class WrapperResolverRegistry:
    """Deterministic exact-host wrapper lookup without network I/O.

    Fully immutable after construction: attribute reassignment is rejected,
    host index is a ``MappingProxyType``, and registrations are a tuple of
    frozen snapshots. Build only via ``from_resolvers()`` / ``empty()``.
    """

    __slots__ = ("_registrations", "_by_host", "_frozen")

    _registrations: tuple[WrapperResolverRegistration, ...]
    _by_host: Mapping[str, WrapperResolverRegistration]
    _frozen: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise WrapperRegistryError(
            "WrapperResolverRegistry must be built via from_resolvers() or empty()"
        )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError("WrapperResolverRegistry is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("WrapperResolverRegistry is immutable")

    @classmethod
    def _create(
        cls,
        registrations: tuple[WrapperResolverRegistration, ...],
        by_host: Mapping[str, WrapperResolverRegistration],
        *,
        seal: object,
    ) -> WrapperResolverRegistry:
        if seal is not _REGISTRY_SEAL:
            raise WrapperRegistryError(
                "WrapperResolverRegistry must be built via from_resolvers() or empty()"
            )
        obj = object.__new__(cls)
        object.__setattr__(obj, "_frozen", False)
        object.__setattr__(obj, "_registrations", registrations)
        object.__setattr__(obj, "_by_host", MappingProxyType(dict(by_host)))
        object.__setattr__(obj, "_frozen", True)
        return obj

    @property
    def registrations(self) -> tuple[WrapperResolverRegistration, ...]:
        return self._registrations

    @property
    def resolvers(self) -> tuple[WrapperResolver, ...]:
        """Resolver objects from registration snapshots (order-preserving)."""
        return tuple(reg.resolver for reg in self._registrations)

    @classmethod
    def from_resolvers(
        cls, resolvers: Sequence[WrapperResolver]
    ) -> WrapperResolverRegistry:
        """Build an immutable registry with fail-closed uniqueness checks.

        Each resolver's ``exact_hostnames``, ``resolver_id``, and
        ``wrapper_type`` are read exactly once and stored on a frozen
        registration snapshot.
        """
        ordered = tuple(resolvers)
        seen_ids: set[str] = set()
        seen_types: set[str] = set()
        by_host: dict[str, WrapperResolverRegistration] = {}
        registrations: list[WrapperResolverRegistration] = []
        for resolver in ordered:
            # Snapshot identity + hosts once; never re-read for policy later.
            resolver_id = resolver.resolver_id
            wrapper_type = resolver.wrapper_type
            if not resolver_id or not isinstance(resolver_id, str):
                raise WrapperRegistryError("resolver_id must be a non-empty string")
            if not wrapper_type or not isinstance(wrapper_type, str):
                raise WrapperRegistryError("wrapper_type must be a non-empty string")
            if resolver_id in seen_ids:
                raise WrapperRegistryError(f"duplicate resolver_id: {resolver_id!r}")
            if wrapper_type in seen_types:
                raise WrapperRegistryError(f"duplicate wrapper_type: {wrapper_type!r}")
            hosts = resolver.exact_hostnames
            if not hosts:
                raise WrapperRegistryError(
                    f"resolver {resolver_id!r} has empty exact_hostnames"
                )
            normalized_hosts: set[str] = set()
            for raw_host in hosts:
                host = _normalize_host(raw_host)
                if host in by_host:
                    raise WrapperRegistryError(
                        f"overlapping wrapper hostname {host!r} between "
                        f"{by_host[host].resolver_id!r} and {resolver_id!r}"
                    )
                if host in normalized_hosts:
                    raise WrapperRegistryError(
                        f"duplicate hostname inside resolver {resolver_id!r}: {host!r}"
                    )
                normalized_hosts.add(host)
            registration = WrapperResolverRegistration(
                resolver=resolver,
                resolver_id=resolver_id,
                wrapper_type=wrapper_type,
                exact_hostnames=frozenset(normalized_hosts),
            )
            for host in registration.exact_hostnames:
                by_host[host] = registration
            registrations.append(registration)
            seen_ids.add(resolver_id)
            seen_types.add(wrapper_type)
        return cls._create(tuple(registrations), by_host, seal=_REGISTRY_SEAL)

    @classmethod
    def empty(cls) -> WrapperResolverRegistry:
        return cls._create((), {}, seal=_REGISTRY_SEAL)

    def find(self, hostname: str) -> WrapperResolverRegistration | None:
        """Exact-host lookup against the immutable registration index."""
        host = hostname.lower().rstrip(".")
        return self._by_host.get(host)

    def match(
        self, validated: SafeDocumentTarget
    ) -> WrapperResolverRegistration | None:
        """Return the registration for a validated wrapper URL, if any."""
        registration = self.find(validated.hostname)
        if registration is None:
            return None
        if not registration.resolver.matches(validated):
            return None
        for other in self._registrations:
            if other is registration:
                continue
            if other.resolver.matches(validated):
                raise WrapperRegistryError(
                    f"ambiguous wrapper match between {registration.resolver_id!r} "
                    f"and {other.resolver_id!r}"
                )
        return registration
