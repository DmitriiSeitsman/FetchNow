"""Neutral stable provider-media identity grammar (VK / Rutube).

Shared by URL validation consumers without importing media_inspection,
resolution, jobs, downloads, or API layers. Callers map
``ProviderIdentityError.reason`` into their own error catalogs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import ParseResult, urlparse, urlunparse

from fetchnow.url.models import ProviderID

# Stable VK identity aliases: /video… and /clip… with the same owner_id shape.
# Positive /video|clip<owner>_<id>; negative /video|clip-<owner>_<id>.
# Reject double-minus, signed video IDs, /clips…, and extra segments.
# Canonical path is always /video{owner}_{id} (never /clip).
_VK_STABLE_PATH = re.compile(r"^/(?:video|clip)(-?\d+)_(\d+)/?$")
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


class ProviderIdentityError(Exception):
    """Fail-closed identity parse failure with a stable reason token."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True, repr=False)
class StableProviderIdentity:
    """Bounded stable media identity derived from a provider URL path."""

    provider_id: str
    media_id: str
    hostname: str
    canonical_path: str

    def __repr__(self) -> str:
        return (
            "StableProviderIdentity("
            f"provider_id={self.provider_id!r}, "
            f"media_id={self.media_id!r}, "
            f"hostname={self.hostname!r})"
        )


def normalize_hostname(hostname: str) -> str:
    host = hostname.strip().lower().rstrip(".")
    if not host or not _SAFE_HOST.fullmatch(host):
        raise ProviderIdentityError("UNSAFE_HOSTNAME")
    if "/" in host or "\\" in host or ".." in host or ":" in host or "%" in host:
        raise ProviderIdentityError("UNSAFE_HOSTNAME")
    return host


def strip_query_fragment(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", "", "")
    )


def parse_stable_provider_identity(
    *,
    provider_id: str,
    hostname: str,
    path: str,
    allowed_hostnames: frozenset[str],
) -> StableProviderIdentity:
    """Parse a stable provider identity from host+path; fail closed otherwise."""
    host = normalize_hostname(hostname)
    if host not in allowed_hostnames:
        raise ProviderIdentityError("HOST_NOT_OWNED")
    raw_path = path or "/"
    path_lower = raw_path.lower()
    if any(marker in path_lower for marker in _FORBIDDEN_PATH_MARKERS):
        raise ProviderIdentityError("FORBIDDEN_MEDIA_PATH")
    if provider_id == ProviderID.VK.value:
        match = _VK_STABLE_PATH.fullmatch(raw_path)
        if match is None:
            raise ProviderIdentityError("VK_PATH_INVALID")
        owner, video = match.group(1), match.group(2)
        media_id = f"{owner}_{video}"
        canonical_path = f"/video{owner}_{video}"
    elif provider_id == ProviderID.RUTUBE.value:
        match = _RUTUBE_STABLE_PATH.fullmatch(raw_path)
        if match is None:
            raise ProviderIdentityError("RUTUBE_PATH_INVALID")
        media_id = match.group(1)
        canonical_path = f"/video/{media_id}/"
    else:
        raise ProviderIdentityError("IDENTITY_PROVIDER_UNSUPPORTED")
    return StableProviderIdentity(
        provider_id=provider_id,
        media_id=media_id,
        hostname=host,
        canonical_path=canonical_path,
    )


def build_canonical_provider_url(*, hostname: str, canonical_path: str) -> str:
    """Trusted HTTPS canonical URL without query/fragment/credentials/port."""
    return build_public_canonical_url(
        scheme="https",
        hostname=hostname,
        port=None,
        canonical_path=canonical_path,
    )


def build_public_canonical_url(
    *,
    scheme: str,
    hostname: str,
    port: int | None,
    canonical_path: str,
) -> str:
    """Build a query/fragment-free canonical URL preserving scheme/port."""
    host = normalize_hostname(hostname)
    path = canonical_path if canonical_path.startswith("/") else f"/{canonical_path}"
    normalized_scheme = scheme.strip().lower()
    if normalized_scheme not in {"http", "https"}:
        raise ProviderIdentityError("IDENTITY_URL_SCHEME")
    netloc = host if port is None else f"{host}:{port}"
    return urlunparse(
        ParseResult(
            scheme=normalized_scheme,
            netloc=netloc,
            path=path,
            params="",
            query="",
            fragment="",
        )
    )


def parse_stable_identity_from_url(
    raw_url: str,
    *,
    provider_id: str,
    allowed_hostnames: frozenset[str],
) -> StableProviderIdentity:
    """Parse identity from an authoritative URL string without retaining it."""
    try:
        parsed = urlparse(raw_url)
    except ValueError as exc:
        raise ProviderIdentityError("IDENTITY_URL_PARSE") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ProviderIdentityError("IDENTITY_URL_CREDENTIALS")
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ProviderIdentityError("IDENTITY_URL_SCHEME")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderIdentityError("IDENTITY_URL_PORT_INVALID") from exc
    if port is not None and port not in (80, 443):
        raise ProviderIdentityError("IDENTITY_URL_PORT_UNTRUSTED")
    if not parsed.hostname:
        raise ProviderIdentityError("IDENTITY_URL_HOST")
    return parse_stable_provider_identity(
        provider_id=provider_id,
        hostname=parsed.hostname,
        path=parsed.path or "/",
        allowed_hostnames=allowed_hostnames,
    )
