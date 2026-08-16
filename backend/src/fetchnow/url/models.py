"""URL validation domain models (FastAPI-free)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderID(StrEnum):
    """Stable provider identifiers."""

    VK = "vk"
    RUTUBE = "rutube"
    OK = "ok"


@dataclass(frozen=True, slots=True)
class NormalizedMediaURL:
    """Safe structured representation of an accepted media URL."""

    original_scheme: str
    scheme: str
    hostname: str
    port: int | None
    path: str
    query: str
    provider_id: str
    canonical: str


@dataclass(frozen=True, slots=True)
class URLValidationResult:
    """Successful validation outcome."""

    provider_id: str
    provider_display_name: str
    url: NormalizedMediaURL
    head_fallback_statuses: frozenset[int] = frozenset()
