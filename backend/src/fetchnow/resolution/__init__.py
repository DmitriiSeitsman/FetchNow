"""Wrapper resolution package (PR3A foundation + PR3B Yandex preview resolver).

Orchestrates transformation of wrapper URLs into canonical media-provider URLs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.errors import ResolutionError, ResolutionErrorKind
from fetchnow.resolution.models import (
    ResolutionHop,
    ResolutionProvenance,
    ResolutionResult,
    WrapperResolveOutcome,
    WrapperValidatedURL,
)
from fetchnow.resolution.protocols import DocumentFetchPort, WrapperResolver
from fetchnow.resolution.registry import (
    WrapperRegistryError,
    WrapperResolverRegistration,
    WrapperResolverRegistry,
)
from fetchnow.resolution.yandex_preview import YandexVideoPreviewResolver

if TYPE_CHECKING:
    from fetchnow.resolution.service import ResolutionService as ResolutionService

__all__ = [
    "DocumentFetchPort",
    "ResolutionError",
    "ResolutionErrorKind",
    "ResolutionHop",
    "ResolutionProvenance",
    "ResolutionResult",
    "ResolutionService",
    "WrapperRegistryError",
    "WrapperResolveOutcome",
    "WrapperResolver",
    "WrapperResolverRegistration",
    "WrapperResolverRegistry",
    "WrapperValidatedURL",
    "YandexVideoPreviewResolver",
    "build_wrapper_registry",
]


def __getattr__(name: str) -> Any:
    if name == "ResolutionService":
        from fetchnow.resolution.service import ResolutionService

        return ResolutionService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
