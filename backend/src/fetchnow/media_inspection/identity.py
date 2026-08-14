"""Stable provider media identity parsing (VK / Rutube only).

Grammar lives in ``fetchnow.url.provider_identity``; this module maps neutral
failures into the media-inspection error catalog.
"""

from __future__ import annotations

from dataclasses import dataclass

from fetchnow.media_inspection.errors import (
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.url.provider_identity import (
    ProviderIdentityError,
    StableProviderIdentity,
    parse_stable_identity_from_url,
    parse_stable_provider_identity,
)
from fetchnow.url.provider_identity import (
    build_canonical_provider_url as _build_canonical_provider_url,
)
from fetchnow.url.provider_identity import (
    normalize_hostname as _normalize_hostname,
)
from fetchnow.url.provider_identity import (
    strip_query_fragment as _strip_query_fragment,
)

# Re-export for callers that already import strip helpers from this module.
strip_query_fragment = _strip_query_fragment


@dataclass(frozen=True, slots=True, repr=False)
class ProviderMediaIdentity:
    """Bounded stable media identity derived from a provider URL path."""

    provider_id: str
    media_id: str
    hostname: str
    canonical_path: str

    def __repr__(self) -> str:
        return (
            "ProviderMediaIdentity("
            f"provider_id={self.provider_id!r}, "
            f"media_id={self.media_id!r}, "
            f"hostname={self.hostname!r})"
        )


def _map_identity_error(exc: ProviderIdentityError) -> None:
    reason = exc.reason
    if reason in {
        "HOST_NOT_OWNED",
        "IDENTITY_URL_CREDENTIALS",
        "IDENTITY_URL_SCHEME",
        "IDENTITY_URL_PORT_UNTRUSTED",
    }:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason=reason,
        )
    if reason == "IDENTITY_PROVIDER_UNSUPPORTED":
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER,
            internal_reason=reason,
        )
    if reason in {
        "IDENTITY_URL_PARSE",
        "IDENTITY_URL_HOST",
        "IDENTITY_URL_PORT_INVALID",
    }:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason=reason,
        )
    raise_inspection_error(
        InspectionErrorKind.INSPECTION_POLICY_REJECTED,
        internal_reason=reason,
    )


def _to_media_identity(identity: StableProviderIdentity) -> ProviderMediaIdentity:
    return ProviderMediaIdentity(
        provider_id=identity.provider_id,
        media_id=identity.media_id,
        hostname=identity.hostname,
        canonical_path=identity.canonical_path,
    )


def normalize_hostname(hostname: str) -> str:
    try:
        return _normalize_hostname(hostname)
    except ProviderIdentityError as exc:
        _map_identity_error(exc)
        raise  # pragma: no cover


def parse_provider_media_identity(
    *,
    provider_id: str,
    hostname: str,
    path: str,
    allowed_hostnames: frozenset[str],
) -> ProviderMediaIdentity:
    """Parse a stable provider identity from host+path; fail closed otherwise."""
    try:
        return _to_media_identity(
            parse_stable_provider_identity(
                provider_id=provider_id,
                hostname=hostname,
                path=path,
                allowed_hostnames=allowed_hostnames,
            )
        )
    except ProviderIdentityError as exc:
        _map_identity_error(exc)
        raise  # pragma: no cover


def build_canonical_provider_url(*, hostname: str, canonical_path: str) -> str:
    """Trusted canonical HTTPS URL without query/fragment/credentials/port."""
    try:
        return _build_canonical_provider_url(
            hostname=hostname,
            canonical_path=canonical_path,
        )
    except ProviderIdentityError as exc:
        _map_identity_error(exc)
        raise  # pragma: no cover


def parse_identity_from_url(
    raw_url: str,
    *,
    provider_id: str,
    allowed_hostnames: frozenset[str],
) -> ProviderMediaIdentity:
    """Parse identity from an authoritative URL string without retaining it."""
    try:
        return _to_media_identity(
            parse_stable_identity_from_url(
                raw_url,
                provider_id=provider_id,
                allowed_hostnames=allowed_hostnames,
            )
        )
    except ProviderIdentityError as exc:
        _map_identity_error(exc)
        raise  # pragma: no cover
