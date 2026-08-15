"""Typed immutable provider capability models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TypeVar

from fetchnow.url.models import ProviderID

CapabilityKey = TypeVar("CapabilityKey", bound=StrEnum)


class CapabilityState(StrEnum):
    """Product state for one provider capability."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    PLANNED = "planned"


class MediaOperation(StrEnum):
    """User-facing media operations."""

    DOWNLOAD_VIDEO = "download_video"
    EXTRACT_AUDIO = "extract_audio"
    SELECT_QUALITY = "select_quality"
    SELECT_CONTAINER = "select_container"


class ContentKind(StrEnum):
    """Provider content kinds relevant to FetchNow."""

    VIDEO = "video"
    CLIP = "clip"
    LIVE = "live"
    PLAYLIST = "playlist"


class MetadataField(StrEnum):
    """Public metadata fields."""

    TITLE = "title"
    DURATION = "duration"
    THUMBNAIL = "thumbnail"


def _complete_mapping(
    values: Mapping[CapabilityKey, CapabilityState],
    *,
    members: type[CapabilityKey],
    field_name: str,
) -> Mapping[CapabilityKey, CapabilityState]:
    expected = set(members)
    if set(values) != expected:
        raise ValueError(f"{field_name} must cover every {members.__name__}")
    if any(not isinstance(value, CapabilityState) for value in values.values()):
        raise ValueError(f"{field_name} values must be CapabilityState")
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Complete immutable product policy for one registered provider."""

    provider_id: ProviderID
    operations: Mapping[MediaOperation, CapabilityState]
    content_kinds: Mapping[ContentKind, CapabilityState]
    metadata: Mapping[MetadataField, CapabilityState]

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, ProviderID):
            raise ValueError("provider_id must be ProviderID")
        object.__setattr__(
            self,
            "operations",
            _complete_mapping(
                self.operations,
                members=MediaOperation,
                field_name="operations",
            ),
        )
        object.__setattr__(
            self,
            "content_kinds",
            _complete_mapping(
                self.content_kinds,
                members=ContentKind,
                field_name="content_kinds",
            ),
        )
        object.__setattr__(
            self,
            "metadata",
            _complete_mapping(
                self.metadata,
                members=MetadataField,
                field_name="metadata",
            ),
        )

    def allows(self, operation: MediaOperation) -> bool:
        """Only enabled operations may enter an executable path."""
        return self.operations[operation] is CapabilityState.ENABLED
