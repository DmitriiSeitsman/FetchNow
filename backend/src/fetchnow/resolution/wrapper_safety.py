"""HTTPS-only wrapper URL safety validation (DNS + destination)."""

from __future__ import annotations

import hashlib

from fetchnow.core.config import Settings
from fetchnow.network.models import SafeDocumentTarget
from fetchnow.resolution.errors import ResolutionErrorKind, raise_resolution_error
from fetchnow.url.destination import (
    assert_addresses_allowed,
    is_reserved_hostname,
    looks_like_nonstandard_ipv4,
    parse_ip_literal,
)
from fetchnow.url.dns import DnsResolver, SystemDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.parse import ParsedURL, parse_and_normalize

# Wrapper layer is HTTPS-only with default port only (no explicit non-default).
_WRAPPER_SCHEMES = frozenset({"https"})
_WRAPPER_PORTS = frozenset({443})


def internal_url_fingerprint(
    *,
    scheme: str,
    hostname: str,
    port: int | None,
    path: str,
    query: str,
) -> str:
    """Process-local hash of the full effective URL for loop detection."""
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    material = (
        f"{scheme.lower()}|{hostname.lower().rstrip('.')}|{effective_port}|"
        f"{path or '/'}|{query}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class WrapperSafetyValidator:
    """Validate wrapper URLs without ProviderRegistry membership."""

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: DnsResolver | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver or SystemDnsResolver(
            timeout_seconds=settings.dns_resolution_timeout_seconds
        )

    def parse_for_classification(self, raw_url: str) -> ParsedURL:
        """Syntactic parse using application scheme/port policy (no DNS)."""
        try:
            return parse_and_normalize(
                raw_url,
                allowed_schemes=set(self._settings.url_allowed_schemes),
                allowed_ports=set(self._settings.url_allowed_ports),
                max_length=self._settings.url_max_length,
            )
        except URLValidationError as exc:
            self._map_parse_error(exc)
            raise  # pragma: no cover - _map_parse_error always raises

    async def validate(self, raw_url: str) -> SafeDocumentTarget:
        """Full wrapper safety validation: HTTPS-only + public DNS."""
        try:
            parsed = parse_and_normalize(
                raw_url,
                allowed_schemes=set(_WRAPPER_SCHEMES),
                allowed_ports=set(_WRAPPER_PORTS),
                max_length=self._settings.url_max_length,
            )
        except URLValidationError as exc:
            self._map_parse_error(exc)
            raise  # pragma: no cover

        if parsed.scheme != "https":
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason="WRAPPER_NON_HTTPS",
            )
        if parsed.port is not None:
            # Explicit non-default ports are forbidden for wrappers.
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason="WRAPPER_NON_DEFAULT_PORT",
            )

        if is_reserved_hostname(parsed.hostname):
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason="RESERVED_HOSTNAME",
            )
        if looks_like_nonstandard_ipv4(parsed.hostname):
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason="NONSTANDARD_IPV4",
            )
        literal = parse_ip_literal(parsed.hostname)
        if literal is not None:
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason="IP_LITERAL",
            )

        try:
            addresses = await self._resolver.resolve(parsed.hostname)
            assert_addresses_allowed(addresses)
        except URLValidationError as exc:
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason=exc.code,
            )

        query = f"?{parsed.query}" if parsed.query else ""
        request_url = f"{parsed.scheme}://{parsed.hostname}{parsed.path}{query}"
        return SafeDocumentTarget(
            scheme=parsed.scheme,
            hostname=parsed.hostname,
            port=parsed.port,
            path=parsed.path,
            query=parsed.query,
            canonical_without_query=parsed.canonical_without_query,
            request_url=request_url,
        )

    @staticmethod
    def _map_parse_error(exc: URLValidationError) -> None:
        if exc.code in {"UNSUPPORTED_SCHEME", "INVALID_URL", "BLOCKED_DESTINATION"}:
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason=exc.code,
            )
        raise_resolution_error(
            ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
            internal_reason=exc.code,
        )
