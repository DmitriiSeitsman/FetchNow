"""URL validation pipeline."""

from __future__ import annotations

import logging
import time

from fetchnow.core.config import Settings
from fetchnow.url.destination import (
    assert_addresses_allowed,
    is_reserved_hostname,
    looks_like_nonstandard_ipv4,
    parse_ip_literal,
)
from fetchnow.url.dns import DnsResolver, SystemDnsResolver
from fetchnow.url.errors import URLValidationError, raise_validation_error
from fetchnow.url.models import NormalizedMediaURL, URLValidationResult
from fetchnow.url.parse import parse_and_normalize
from fetchnow.url.providers import ProviderRegistry

logger = logging.getLogger("fetchnow.url.validate")


class URLValidator:
    """Validate and normalize user-supplied media URLs."""

    def __init__(
        self,
        settings: Settings,
        *,
        registry: ProviderRegistry | None = None,
        resolver: DnsResolver | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry or ProviderRegistry.from_settings(settings)
        self._resolver = resolver or SystemDnsResolver(
            timeout_seconds=settings.dns_resolution_timeout_seconds
        )

    async def validate(self, raw_url: str) -> URLValidationResult:
        """Run the full validation pipeline."""
        started = time.perf_counter()
        provider_id: str | None = None
        hostname: str | None = None
        error_code: str | None = None
        try:
            result = await self._validate_inner(raw_url)
            provider_id = result.provider_id
            hostname = result.url.hostname
        except URLValidationError as exc:
            error_code = exc.code
            self._log_outcome(
                started,
                provider_id=provider_id,
                hostname=hostname,
                error_code=error_code,
            )
            raise
        except Exception:
            error_code = "INTERNAL_ERROR"
            self._log_outcome(
                started,
                provider_id=provider_id,
                hostname=hostname,
                error_code=error_code,
            )
            raise_validation_error("INTERNAL_ERROR")
        else:
            self._log_outcome(
                started,
                provider_id=provider_id,
                hostname=hostname,
                error_code=None,
            )
            return result

    def _log_outcome(
        self,
        started: float,
        *,
        provider_id: str | None,
        hostname: str | None,
        error_code: str | None,
    ) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "url_validation_complete",
            extra={
                "outcome": "error" if error_code else "allow",
                "provider_id": provider_id,
                "hostname": hostname,
                "error_code": error_code,
                "duration_ms": duration_ms,
            },
        )

    async def _validate_inner(self, raw_url: str) -> URLValidationResult:
        parsed = parse_and_normalize(
            raw_url,
            allowed_schemes=set(self._settings.url_allowed_schemes),
            allowed_ports=set(self._settings.url_allowed_ports),
            max_length=self._settings.url_max_length,
        )

        # Block reserved names and nonstandard / literal IPs before provider DNS.
        if is_reserved_hostname(parsed.hostname):
            raise_validation_error("BLOCKED_DESTINATION")

        if looks_like_nonstandard_ipv4(parsed.hostname):
            raise_validation_error("BLOCKED_DESTINATION")

        literal = parse_ip_literal(parsed.hostname)
        if literal is not None:
            assert_addresses_allowed([literal])
            # Literal IPs are never part of the provider hostname allowlist.
            raise_validation_error("UNSUPPORTED_PROVIDER")

        provider = self._registry.resolve(parsed.hostname)

        addresses = await self._resolver.resolve(parsed.hostname)
        assert_addresses_allowed(addresses)

        normalized = NormalizedMediaURL(
            original_scheme=parsed.original_scheme,
            scheme=parsed.scheme,
            hostname=parsed.hostname,
            port=parsed.port,
            path=parsed.path,
            query=parsed.query,
            provider_id=provider.id,
            canonical=parsed.canonical_without_query,
        )
        return URLValidationResult(
            provider_id=provider.id,
            provider_display_name=provider.display_name,
            url=normalized,
        )
