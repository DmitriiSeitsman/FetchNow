"""Rebuild a validated ResolutionResult from persisted job target fields."""

from __future__ import annotations

from fetchnow.core.config import Settings
from fetchnow.jobs.errors import JobErrorCode, raise_job_error
from fetchnow.media_inspection.identity import (
    normalize_hostname,
    parse_provider_media_identity,
)
from fetchnow.media_inspection.registry import (
    DEFAULT_YTDLP_EXTRACTOR_KEYS,
    InspectionExtractorRegistry,
)
from fetchnow.resolution.errors import (
    ResolutionErrorKind,
    raise_resolution_error,
)
from fetchnow.resolution.models import ResolutionProvenance, ResolutionResult
from fetchnow.url.errors import URLValidationError
from fetchnow.url.models import NormalizedMediaURL, URLValidationResult
from fetchnow.url.parse import parse_and_normalize
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator


async def rebuild_resolution_result(
    *,
    provider_id: str,
    canonical_provider_url: str,
    media_id: str,
    hostname: str,
    path: str,
    scheme: str,
    port: int | None,
    settings: Settings,
    inspection_registry: InspectionExtractorRegistry | None = None,
    provider_registry: ProviderRegistry | None = None,
    validator: URLValidator | None = None,
) -> ResolutionResult:
    """Rebuild ResolutionResult from trusted-looking DB fields after re-validation.

    Never accepts wrapper URLs. ``submitted_url`` is set to the terminal
    canonical only. Optionally re-runs DNS via ``URLValidator`` when provided.
    """
    if "?" in canonical_provider_url or "#" in canonical_provider_url:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_QUERY_OR_FRAGMENT",
        )
    if path and ("?" in path or "#" in path):
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_PATH_QUERY_OR_FRAGMENT",
        )

    allowed_schemes = {s.lower() for s in settings.url_allowed_schemes}
    allowed_ports = set(settings.url_allowed_ports)

    try:
        parsed = parse_and_normalize(
            canonical_provider_url,
            allowed_schemes=allowed_schemes,
            allowed_ports=allowed_ports,
            max_length=settings.url_max_length,
        )
    except URLValidationError:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_CANONICAL_REPARSE",
        )

    if parsed.query:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_QUERY_PRESENT",
        )
    if parsed.scheme != scheme.strip().lower():
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_SCHEME_MISMATCH",
        )
    if parsed.hostname != hostname.strip().lower().rstrip("."):
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_HOST_MISMATCH",
        )
    if parsed.port != port:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_PORT_MISMATCH",
        )
    if parsed.path != path:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_PATH_MISMATCH",
        )
    if parsed.canonical_without_query != canonical_provider_url:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_CANONICAL_MISMATCH",
        )

    pid = provider_id.strip().lower()
    host = normalize_hostname(hostname)

    # Ownership: inspection registry preferred; else provider registry + defaults.
    owned = False
    allowed_hosts: frozenset[str] | None = None
    display_name = pid
    if inspection_registry is not None:
        try:
            binding = inspection_registry.resolve(provider_id=pid, hostname=host)
            owned = True
            allowed_hosts = binding.exact_hostnames
        except Exception:
            owned = False
    if not owned and provider_registry is not None:
        descriptor = provider_registry.find(host)
        if (
            descriptor is not None
            and descriptor.enabled
            and descriptor.id == pid
            and pid in DEFAULT_YTDLP_EXTRACTOR_KEYS
        ):
            owned = True
            allowed_hosts = descriptor.exact_hostnames
            display_name = descriptor.display_name
    if not owned:
        raise_resolution_error(
            ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
            internal_reason="PERSISTED_PROVIDER_NOT_OWNED",
        )
    assert allowed_hosts is not None

    if provider_registry is not None:
        descriptor = provider_registry.find(host)
        if descriptor is not None:
            display_name = descriptor.display_name

    identity = parse_provider_media_identity(
        provider_id=pid,
        hostname=host,
        path=parsed.path or "/",
        allowed_hostnames=allowed_hosts,
    )
    if identity.media_id != media_id:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="PERSISTED_MEDIA_ID_MISMATCH",
        )

    if validator is not None:
        try:
            validated = await validator.validate(canonical_provider_url)
        except URLValidationError:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="PERSISTED_DNS_VALIDATION",
            )
        if validated.provider_id != pid:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="PERSISTED_VALIDATED_PROVIDER",
            )
        if validated.url.canonical != canonical_provider_url:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="PERSISTED_VALIDATED_CANONICAL",
            )
        display_name = validated.provider_display_name
    else:
        normalized = NormalizedMediaURL(
            original_scheme=parsed.original_scheme,
            scheme=parsed.scheme,
            hostname=parsed.hostname,
            port=parsed.port,
            path=parsed.path,
            query="",
            provider_id=pid,
            canonical=canonical_provider_url,
        )
        validated = URLValidationResult(
            provider_id=pid,
            provider_display_name=display_name,
            url=normalized,
        )

    # Trusted terminal only — never pass wrapper/submitted URLs into inspection.
    return ResolutionResult(
        submitted_url=canonical_provider_url,
        validated=validated,
        provider_id=pid,
        provider_display_name=display_name,
        canonical_provider_url=canonical_provider_url,
        wrapper_type=None,
        provenance=ResolutionProvenance.DIRECT_PROVIDER,
        resolution_chain=(),
    )
