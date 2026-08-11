"""Immutable wrapper-resolution domain models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from fetchnow.network.models import SafeDocumentTarget
from fetchnow.url.models import NormalizedMediaURL, URLValidationResult

# Domain alias: resolution talks about wrapper URLs; network owns the type so
# SafeHTTPClient does not import the resolution package.
WrapperValidatedURL = SafeDocumentTarget

# Audit identifiers that appear in hop/result repr must be token-shaped.
_SAFE_AUDIT_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_MAX_METADATA_KEYS = 8
_MAX_METADATA_KEY_LEN = 32
_MAX_METADATA_SERIALIZED = 256
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "cookie",
        "set-cookie",
        "query",
        "location",
        "url",
        "body",
        "header",
        "headers",
        "cookie_jar",
        "auth",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "signature",
        "signed",
    }
)


class ResolutionProvenance(StrEnum):
    """How the terminal provider URL was obtained."""

    DIRECT_PROVIDER = "direct_provider"
    WRAPPER_RESOLVED = "wrapper_resolved"


def require_audit_token(value: str, *, field_name: str) -> str:
    """Accept only lowercase identifier tokens for audit/repr surfaces."""
    if not isinstance(value, str) or not _SAFE_AUDIT_TOKEN.fullmatch(value):
        raise ValueError(f"unsafe {field_name} for resolution audit")
    return value


def freeze_resolution_metadata(
    raw: Mapping[str, Any] | None,
) -> tuple[tuple[str, int | bool], ...]:
    """Validate and freeze hop metadata under a strict scalar-only contract.

    Strings are rejected: resolver-controlled text cannot be guaranteed free of
    secrets, and metadata is excluded from ``ResolutionHop.__repr__`` as a
    second line of defense. Keys are sorted so the result is deterministic
    across process hash seeds and input mapping order.
    """
    if raw is None:
        return ()
    if len(raw) > _MAX_METADATA_KEYS:
        raise ValueError("resolution metadata exceeds key limit")
    items: dict[str, int | bool] = {}
    serialized = 0
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError("resolution metadata keys must be strings")
        normalized_key = key.strip().lower()
        if not normalized_key or len(normalized_key) > _MAX_METADATA_KEY_LEN:
            raise ValueError("resolution metadata key length invalid")
        if not _SAFE_AUDIT_TOKEN.fullmatch(normalized_key):
            raise ValueError("resolution metadata key is not a safe token")
        if normalized_key in _FORBIDDEN_METADATA_KEYS:
            raise ValueError("resolution metadata key is forbidden")
        if isinstance(value, bool):
            coerced: int | bool = value
            serialized += len(normalized_key) + 5
        elif isinstance(value, int) and not isinstance(value, bool):
            coerced = value
            serialized += len(normalized_key) + len(str(value))
        else:
            raise ValueError("resolution metadata values must be int|bool")
        if serialized > _MAX_METADATA_SERIALIZED:
            raise ValueError("resolution metadata exceeds size limit")
        if normalized_key in items:
            raise ValueError("resolution metadata key collision after normalize")
        items[normalized_key] = coerced
    return tuple(sorted(items.items(), key=lambda pair: pair[0]))


@dataclass(frozen=True, slots=True, repr=False)
class ResolutionHop:
    """One safe audit hop in a wrapper-resolution chain."""

    resolver_id: str
    wrapper_type: str
    source_canonical: str
    target_canonical: str
    strategy: str
    metadata: tuple[tuple[str, int | bool], ...] = field(
        default_factory=tuple, repr=False
    )

    def __post_init__(self) -> None:
        require_audit_token(self.resolver_id, field_name="resolver_id")
        require_audit_token(self.wrapper_type, field_name="wrapper_type")
        require_audit_token(self.strategy, field_name="strategy")

    def __repr__(self) -> str:
        # Metadata is never rendered — even bounded scalars stay out of logs.
        return (
            "ResolutionHop("
            f"resolver_id={self.resolver_id!r}, "
            f"wrapper_type={self.wrapper_type!r}, "
            f"source_canonical={self.source_canonical!r}, "
            f"target_canonical={self.target_canonical!r}, "
            f"strategy={self.strategy!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ResolutionResult:
    """Terminal resolution outcome with privacy-sensitive fields hidden from repr."""

    # Sensitive / internal — never auto-logged or repr'd.
    submitted_url: str = field(repr=False)
    validated: URLValidationResult = field(repr=False)
    # Public-safe terminal identity.
    provider_id: str
    provider_display_name: str
    canonical_provider_url: str
    wrapper_type: str | None
    provenance: ResolutionProvenance
    resolution_chain: tuple[ResolutionHop, ...] = ()

    @property
    def provider_url(self) -> NormalizedMediaURL:
        """Validated provider URL object (may retain internal query)."""
        return self.validated.url

    def __repr__(self) -> str:
        return (
            "ResolutionResult("
            f"provider_id={self.provider_id!r}, "
            f"canonical_provider_url={self.canonical_provider_url!r}, "
            f"wrapper_type={self.wrapper_type!r}, "
            f"provenance={self.provenance!r}, "
            f"chain_len={len(self.resolution_chain)})"
        )


@dataclass(frozen=True, slots=True)
class WrapperResolveOutcome:
    """Candidate URL produced by a single wrapper resolver hop."""

    candidate_url: str = field(repr=False)
    strategy: str
    metadata: Mapping[str, Any] | None = None

    def __repr__(self) -> str:
        if isinstance(self.strategy, str) and _SAFE_AUDIT_TOKEN.fullmatch(
            self.strategy
        ):
            strategy = self.strategy
        else:
            strategy = "redacted"
        return f"WrapperResolveOutcome(strategy={strategy!r})"
