"""Factory helpers for media inspection wiring."""

from __future__ import annotations

from fetchnow.core.config import Settings
from fetchnow.media_inspection.protocols import ProcessRunner
from fetchnow.media_inspection.registry import (
    DEFAULT_YTDLP_EXTRACTOR_KEYS,
    InspectionExtractorRegistry,
)
from fetchnow.media_inspection.service import MediaInspectionService
from fetchnow.media_inspection.ytdlp_adapter import YtDlpMetadataExtractor
from fetchnow.url.models import ProviderID
from fetchnow.url.providers import ProviderRegistry


def build_default_inspection_registry(
    settings: Settings,
    providers: ProviderRegistry,
    *,
    runner: ProcessRunner | None = None,
) -> InspectionExtractorRegistry:
    """Build an immutable VK/Rutube/OK inspection registry from provider ownership."""
    extractors = {
        ProviderID.VK.value: YtDlpMetadataExtractor(
            settings=settings,
            allowed_extractor_keys=DEFAULT_YTDLP_EXTRACTOR_KEYS[ProviderID.VK.value],
            runner=runner,
        ),
        ProviderID.RUTUBE.value: YtDlpMetadataExtractor(
            settings=settings,
            allowed_extractor_keys=DEFAULT_YTDLP_EXTRACTOR_KEYS[
                ProviderID.RUTUBE.value
            ],
            runner=runner,
        ),
        ProviderID.OK.value: YtDlpMetadataExtractor(
            settings=settings,
            allowed_extractor_keys=DEFAULT_YTDLP_EXTRACTOR_KEYS[ProviderID.OK.value],
            runner=runner,
        ),
    }
    return InspectionExtractorRegistry.from_provider_registry(
        providers,
        extractors_by_provider=extractors,
        allowed_extractor_keys=DEFAULT_YTDLP_EXTRACTOR_KEYS,
    )


def build_media_inspection_service(
    settings: Settings,
    providers: ProviderRegistry,
    *,
    runner: ProcessRunner | None = None,
) -> MediaInspectionService:
    registry = build_default_inspection_registry(
        settings, providers, runner=runner
    )
    return MediaInspectionService(settings=settings, registry=registry)
