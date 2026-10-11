"""Explicit release image-set mode (SEC-08/09).

Source contract version governs which files must exist and be hashed.
release_mode governs which images are built and recorded — never inferred
from accidental file presence.
"""

from __future__ import annotations

from .c2_constants import (
    APPLICATION_BUILD_SERVICES,
    MEDIA_BUILD_SERVICES,
    SOURCE_CONTRACT_VERSION_V4,
)

RELEASE_MODE_CANONICAL = "canonical"
RELEASE_MODE_MEDIA = "media"
SUPPORTED_RELEASE_MODES = frozenset({RELEASE_MODE_CANONICAL, RELEASE_MODE_MEDIA})

MEDIA_SERVICE_IMAGE_REFS = {
    "media-executor": "fetchnow-media-executor",
    "media-net-executor": "fetchnow-media-net-executor",
    "egress-proxy": "fetchnow-media-egress-proxy",
}

CANONICAL_SERVICE_IMAGE_REFS = {
    "api": "fetchnow-api",
    "web": "fetchnow-web",
    "gateway": "fetchnow-gateway",
}


class ReleaseModeError(ValueError):
    """Invalid or unsupported release mode."""


def parse_release_mode(
    raw: object,
    *,
    source_contract_version: int,
    required: bool = False,
) -> str:
    """Parse release_mode from a manifest or CLI value.

    Legacy manifests (v1–v3) omit the field and default to canonical.
    v4 prepares always record an explicit mode.
    """
    if raw is None:
        if required or source_contract_version >= SOURCE_CONTRACT_VERSION_V4:
            raise ReleaseModeError("release_mode is required for v4 manifests")
        return RELEASE_MODE_CANONICAL
    if type(raw) is not str:
        raise ReleaseModeError("release_mode must be a string")
    if raw not in SUPPORTED_RELEASE_MODES:
        raise ReleaseModeError(f"unsupported release_mode: {raw!r}")
    if (
        raw == RELEASE_MODE_MEDIA
        and source_contract_version < SOURCE_CONTRACT_VERSION_V4
    ):
        raise ReleaseModeError(
            "release_mode=media requires source_contract_version >= 4"
        )
    return raw


def build_services_for_mode(release_mode: str) -> tuple[str, ...]:
    mode = parse_release_mode(
        release_mode,
        source_contract_version=SOURCE_CONTRACT_VERSION_V4,
        required=True,
    )
    if mode == RELEASE_MODE_CANONICAL:
        return APPLICATION_BUILD_SERVICES
    return APPLICATION_BUILD_SERVICES + MEDIA_BUILD_SERVICES


def image_reference_for_service(service: str, revision: str) -> str:
    if service in CANONICAL_SERVICE_IMAGE_REFS:
        return f"{CANONICAL_SERVICE_IMAGE_REFS[service]}:{revision}"
    if service in MEDIA_SERVICE_IMAGE_REFS:
        return f"{MEDIA_SERVICE_IMAGE_REFS[service]}:{revision}"
    raise ReleaseModeError(f"unknown build service: {service!r}")


def expected_image_services(release_mode: str) -> frozenset[str]:
    return frozenset(build_services_for_mode(release_mode))


def target_image_refs_for_mode(revision: str, release_mode: str) -> tuple[str, ...]:
    return tuple(
        image_reference_for_service(svc, revision)
        for svc in build_services_for_mode(release_mode)
    )
