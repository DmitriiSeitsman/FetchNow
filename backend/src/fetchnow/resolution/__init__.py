"""Wrapper resolution foundation (PR3A).

FastAPI-independent orchestration for transforming wrapper URLs into
canonical media-provider URLs. No production wrapper resolvers ship here;
synthetic resolvers belong in tests only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
]


def __getattr__(name: str) -> Any:
    if name == "ResolutionService":
        from fetchnow.resolution.service import ResolutionService

        return ResolutionService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
