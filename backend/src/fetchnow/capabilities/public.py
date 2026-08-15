"""Public API projection for provider capabilities (PR-B).

Decouples wire JSON from internal ``ProviderCapabilities`` / enum identity.
"""

from __future__ import annotations

from typing import Any

from fetchnow.capabilities.models import (
    CapabilityState,
    ContentKind,
    MediaOperation,
    MetadataField,
    ProviderCapabilities,
)
from fetchnow.capabilities.registry import ProviderCapabilityRegistry

# Explicit public wire keys — not Python enum member names.
_OPERATION_KEYS: tuple[tuple[MediaOperation, str], ...] = (
    (MediaOperation.DOWNLOAD_VIDEO, "downloadVideo"),
    (MediaOperation.EXTRACT_AUDIO, "extractAudio"),
    (MediaOperation.SELECT_QUALITY, "selectQuality"),
    (MediaOperation.SELECT_CONTAINER, "selectContainer"),
)
_CONTENT_KIND_KEYS: tuple[tuple[ContentKind, str], ...] = (
    (ContentKind.VIDEO, "video"),
    (ContentKind.CLIP, "clip"),
    (ContentKind.LIVE, "live"),
    (ContentKind.PLAYLIST, "playlist"),
)
_METADATA_KEYS: tuple[tuple[MetadataField, str], ...] = (
    (MetadataField.TITLE, "title"),
    (MetadataField.DURATION, "duration"),
    (MetadataField.THUMBNAIL, "thumbnail"),
)


def _state_wire(state: CapabilityState) -> str:
    return str(state.value)


def _project_descriptor(descriptor: ProviderCapabilities) -> dict[str, Any]:
    return {
        "providerId": str(descriptor.provider_id.value),
        "operations": {
            wire: _state_wire(descriptor.operations[op])
            for op, wire in _OPERATION_KEYS
        },
        "contentKinds": {
            wire: _state_wire(descriptor.content_kinds[kind])
            for kind, wire in _CONTENT_KIND_KEYS
        },
        "metadata": {
            wire: _state_wire(descriptor.metadata[field])
            for field, wire in _METADATA_KEYS
        },
    }


def project_provider_capabilities(
    registry: ProviderCapabilityRegistry,
    provider_id: str,
) -> dict[str, Any] | None:
    """Return a camelCase public capability object, or ``None`` when unknown.

    Soft-lookup only — never raises download errors on read/projection paths.
    """
    descriptor = registry.get(provider_id)
    if descriptor is None:
        return None
    return _project_descriptor(descriptor)
