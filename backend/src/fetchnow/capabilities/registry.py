"""Immutable canonical registry for provider capability policy."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from fetchnow.capabilities.models import (
    CapabilityState,
    ContentKind,
    MediaOperation,
    MetadataField,
    ProviderCapabilities,
)
from fetchnow.url.models import ProviderID


def _capabilities(provider_id: ProviderID) -> ProviderCapabilities:
    # Shared product policy for providers that expose ordinary + short-form
    # public video download with quality selection. Live metadata probing
    # confirmed progressive download + quality selection remain valid defaults;
    # extract-audio / select-container stay disabled.
    return ProviderCapabilities(
        provider_id=provider_id,
        operations={
            MediaOperation.DOWNLOAD_VIDEO: CapabilityState.ENABLED,
            MediaOperation.EXTRACT_AUDIO: CapabilityState.DISABLED,
            MediaOperation.SELECT_QUALITY: CapabilityState.ENABLED,
            MediaOperation.SELECT_CONTAINER: CapabilityState.DISABLED,
        },
        content_kinds={
            ContentKind.VIDEO: CapabilityState.ENABLED,
            ContentKind.CLIP: CapabilityState.ENABLED,
            ContentKind.LIVE: CapabilityState.DISABLED,
            ContentKind.PLAYLIST: CapabilityState.DISABLED,
        },
        metadata={
            MetadataField.TITLE: CapabilityState.ENABLED,
            MetadataField.DURATION: CapabilityState.ENABLED,
            MetadataField.THUMBNAIL: CapabilityState.PLANNED,
        },
    )


def _ok_capabilities() -> ProviderCapabilities:
    # OK.ru has no confirmed separate clip/short-form URL family. Public
    # progressive options are derived from DASH webm A/V via muxing when
    # MEDIA_MUXING_ENABLED is on (confirmed live against odnoklassniki IE).
    return ProviderCapabilities(
        provider_id=ProviderID.OK,
        operations={
            MediaOperation.DOWNLOAD_VIDEO: CapabilityState.ENABLED,
            MediaOperation.EXTRACT_AUDIO: CapabilityState.DISABLED,
            MediaOperation.SELECT_QUALITY: CapabilityState.ENABLED,
            MediaOperation.SELECT_CONTAINER: CapabilityState.DISABLED,
        },
        content_kinds={
            ContentKind.VIDEO: CapabilityState.ENABLED,
            ContentKind.CLIP: CapabilityState.DISABLED,
            ContentKind.LIVE: CapabilityState.DISABLED,
            ContentKind.PLAYLIST: CapabilityState.DISABLED,
        },
        metadata={
            MetadataField.TITLE: CapabilityState.ENABLED,
            MetadataField.DURATION: CapabilityState.ENABLED,
            MetadataField.THUMBNAIL: CapabilityState.PLANNED,
        },
    )


def _dzen_capabilities() -> ProviderCapabilities:
    # Dzen /shorts/{id} is an input alias of /video/watch/{id} (same media id;
    # ZenYandex IE). Public progressive options require muxing of DASH A/V
    # (avc + aac) when MEDIA_MUXING_ENABLED is on — no muxed progressive rows
    # from the extractor. Live probe confirmed multiple heights and quality
    # selection; no extract-audio / container choice observed.
    return ProviderCapabilities(
        provider_id=ProviderID.DZEN,
        operations={
            MediaOperation.DOWNLOAD_VIDEO: CapabilityState.ENABLED,
            MediaOperation.EXTRACT_AUDIO: CapabilityState.DISABLED,
            MediaOperation.SELECT_QUALITY: CapabilityState.ENABLED,
            MediaOperation.SELECT_CONTAINER: CapabilityState.DISABLED,
        },
        content_kinds={
            ContentKind.VIDEO: CapabilityState.ENABLED,
            ContentKind.CLIP: CapabilityState.ENABLED,
            ContentKind.LIVE: CapabilityState.DISABLED,
            ContentKind.PLAYLIST: CapabilityState.DISABLED,
        },
        metadata={
            MetadataField.TITLE: CapabilityState.ENABLED,
            MetadataField.DURATION: CapabilityState.ENABLED,
            MetadataField.THUMBNAIL: CapabilityState.PLANNED,
        },
    )


DEFAULT_PROVIDER_CAPABILITIES: Mapping[ProviderID, ProviderCapabilities] = (
    MappingProxyType(
        {
            ProviderID.VK: _capabilities(ProviderID.VK),
            ProviderID.RUTUBE: _capabilities(ProviderID.RUTUBE),
            ProviderID.OK: _ok_capabilities(),
            ProviderID.DZEN: _dzen_capabilities(),
        }
    )
)


class ProviderCapabilityRegistry:
    """Fail-closed immutable provider capability lookup."""

    __slots__ = ("_capabilities", "_frozen")

    _capabilities: Mapping[ProviderID, ProviderCapabilities]
    _frozen: bool

    def __init__(self, capabilities: Mapping[ProviderID, ProviderCapabilities]) -> None:
        object.__setattr__(self, "_capabilities", MappingProxyType(dict(capabilities)))
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError("ProviderCapabilityRegistry is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def default(cls) -> ProviderCapabilityRegistry:
        """Return the sealed built-in provider capability registry."""
        return DEFAULT_CAPABILITY_REGISTRY

    @classmethod
    def from_mapping(
        cls, capabilities: Mapping[ProviderID, ProviderCapabilities]
    ) -> ProviderCapabilityRegistry:
        """Validate a complete provider-keyed matrix and seal it."""
        if set(capabilities) != set(ProviderID):
            raise ValueError("capability registry must cover every ProviderID")
        for provider_id, descriptor in capabilities.items():
            if provider_id is not descriptor.provider_id:
                raise ValueError("capability registry key does not match provider_id")
        return cls(capabilities)

    def get(self, provider_id: str) -> ProviderCapabilities | None:
        """Return a descriptor when the external provider id is known."""
        try:
            normalized = ProviderID(provider_id.strip().lower())
        except (AttributeError, ValueError):
            return None
        return self._capabilities.get(normalized)

    def require(self, provider_id: str) -> ProviderCapabilities:
        """Resolve a provider capability descriptor or fail closed."""
        descriptor = self.get(provider_id)
        if descriptor is None:
            from fetchnow.downloads.errors import (
                DownloadErrorCode,
                raise_download_error,
            )

            raise_download_error(
                DownloadErrorCode.PROVIDER_CAPABILITY_DISABLED,
                internal_reason="CAPABILITY_PROVIDER_UNKNOWN",
            )
        return descriptor


DEFAULT_CAPABILITY_REGISTRY = ProviderCapabilityRegistry.from_mapping(
    DEFAULT_PROVIDER_CAPABILITIES
)


def assert_operation_allowed(
    capabilities: ProviderCapabilityRegistry,
    *,
    provider_id: str,
    operation: MediaOperation,
) -> None:
    """Allow one enabled provider operation; reject all other states."""
    descriptor = capabilities.require(provider_id)
    if not descriptor.allows(operation):
        from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error

        raise_download_error(
            DownloadErrorCode.PROVIDER_CAPABILITY_DISABLED,
            internal_reason="CAPABILITY_OPERATION_DISABLED",
        )
