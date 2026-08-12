"""Stable provider media identity parsing (VK / Rutube only)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from fetchnow.media_inspection.errors import (
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.url.models import ProviderID

# Positive /video<owner>_<id>; negative /video-<owner>_<id>. Reject /video--…
_VK_STABLE_PATH = re.compile(r"^/video(-?\d+)_(\d+)/?$")
_RUTUBE_STABLE_PATH = re.compile(r"^/video/([A-Za-z0-9_-]{1,64})/?$")
_SAFE_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_FORBIDDEN_PATH_MARKERS = (
    "/video_ext.php",
    "/video_ext",
    "/embed",
    ".m3u8",
    ".mpd",
    ".mp4",
    ".webm",
)


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


def normalize_hostname(hostname: str) -> str:
    host = hostname.strip().lower().rstrip(".")
    if not host or not _SAFE_HOST.fullmatch(host):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="UNSAFE_HOSTNAME",
        )
    if "/" in host or "\\" in host or ".." in host or ":" in host or "%" in host:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="UNSAFE_HOSTNAME",
        )
    return host


def strip_query_fragment(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", "", "")
    )


def parse_provider_media_identity(
    *,
    provider_id: str,
    hostname: str,
    path: str,
    allowed_hostnames: frozenset[str],
) -> ProviderMediaIdentity:
    """Parse a stable provider identity from host+path; fail closed otherwise."""
    host = normalize_hostname(hostname)
    if host not in allowed_hostnames:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="HOST_NOT_OWNED",
        )
    raw_path = path or "/"
    path_lower = raw_path.lower()
    if any(marker in path_lower for marker in _FORBIDDEN_PATH_MARKERS):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="FORBIDDEN_MEDIA_PATH",
        )
    if provider_id == ProviderID.VK.value:
        match = _VK_STABLE_PATH.fullmatch(raw_path)
        if match is None:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="VK_PATH_INVALID",
            )
        owner, video = match.group(1), match.group(2)
        media_id = f"{owner}_{video}"
        canonical_path = f"/video{owner}_{video}"
    elif provider_id == ProviderID.RUTUBE.value:
        match = _RUTUBE_STABLE_PATH.fullmatch(raw_path)
        if match is None:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="RUTUBE_PATH_INVALID",
            )
        media_id = match.group(1)
        canonical_path = f"/video/{media_id}/"
    else:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER,
            internal_reason="IDENTITY_PROVIDER_UNSUPPORTED",
        )
    return ProviderMediaIdentity(
        provider_id=provider_id,
        media_id=media_id,
        hostname=host,
        canonical_path=canonical_path,
    )


def build_canonical_provider_url(*, hostname: str, canonical_path: str) -> str:
    """Trusted canonical HTTPS URL without query/fragment/credentials/port."""
    host = normalize_hostname(hostname)
    path = canonical_path if canonical_path.startswith("/") else f"/{canonical_path}"
    return f"https://{host}{path}"


def parse_identity_from_url(
    raw_url: str,
    *,
    provider_id: str,
    allowed_hostnames: frozenset[str],
) -> ProviderMediaIdentity:
    """Parse identity from an authoritative URL string without retaining it."""
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="IDENTITY_URL_PARSE",
        )
    if parsed.username is not None or parsed.password is not None:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="IDENTITY_URL_CREDENTIALS",
        )
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="IDENTITY_URL_SCHEME",
        )
    try:
        port = parsed.port
    except ValueError:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="IDENTITY_URL_PORT",
        )
    if port is not None and port not in (80, 443):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="IDENTITY_URL_PORT",
        )
    if not parsed.hostname:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="IDENTITY_URL_HOST",
        )
    return parse_provider_media_identity(
        provider_id=provider_id,
        hostname=parsed.hostname,
        path=parsed.path or "/",
        allowed_hostnames=allowed_hostnames,
    )
