"""Typed media-inspection domain errors (FastAPI-independent)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import NoReturn

# Process-local diagnostics only. Arbitrary tool text is rejected so secrets
# never linger on the exception object for logging or repr.
_SAFE_INTERNAL_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class InspectionErrorKind(StrEnum):
    """Lower-case domain outcomes for media inspection.

    These are not public API error codes. Public mapping is deferred until a
    job-backed endpoint exists (PR5+). Catalog messages stay URL/tool-free.
    """

    INSPECTION_UNSUPPORTED_PROVIDER = "inspection_unsupported_provider"
    INSPECTION_TOOL_UNAVAILABLE = "inspection_tool_unavailable"
    INSPECTION_TIMEOUT = "inspection_timeout"
    INSPECTION_OUTPUT_LIMIT_EXCEEDED = "inspection_output_limit_exceeded"
    INSPECTION_INVALID_OUTPUT = "inspection_invalid_output"
    INSPECTION_PROVIDER_MISMATCH = "inspection_provider_mismatch"
    INSPECTION_MEDIA_UNAVAILABLE = "inspection_media_unavailable"
    INSPECTION_POLICY_REJECTED = "inspection_policy_rejected"
    INSPECTION_CANCELLED = "inspection_cancelled"
    INTERNAL_INSPECTION_ERROR = "internal_inspection_error"


_PUBLIC_MESSAGES: dict[InspectionErrorKind, str] = {
    InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER: (
        "Media inspection is not available for this provider."
    ),
    InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE: (
        "Media inspection is not configured on this server."
    ),
    InspectionErrorKind.INSPECTION_TIMEOUT: "Media inspection timed out.",
    InspectionErrorKind.INSPECTION_OUTPUT_LIMIT_EXCEEDED: (
        "Media inspection output exceeded allowed limits."
    ),
    InspectionErrorKind.INSPECTION_INVALID_OUTPUT: (
        "Media inspection returned invalid metadata."
    ),
    InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH: (
        "Media inspection result did not match the expected provider."
    ),
    InspectionErrorKind.INSPECTION_MEDIA_UNAVAILABLE: (
        "The media could not be inspected."
    ),
    InspectionErrorKind.INSPECTION_POLICY_REJECTED: (
        "The media violates inspection policy limits."
    ),
    InspectionErrorKind.INSPECTION_CANCELLED: "Media inspection was cancelled.",
    InspectionErrorKind.INTERNAL_INSPECTION_ERROR: (
        "An unexpected media inspection error occurred."
    ),
}


def sanitize_internal_reason(raw: str | None) -> str | None:
    """Accept only short uppercase diagnostic codes; drop everything else."""
    if raw is None:
        return None
    if _SAFE_INTERNAL_REASON.fullmatch(raw):
        return raw
    return "REDACTED_INTERNAL_REASON"


class InspectionError(Exception):
    """Fail-closed inspection failure with a stable public-safe message."""

    def __init__(
        self,
        kind: InspectionErrorKind,
        *,
        message: str | None = None,
        internal_reason: str | None = None,
    ) -> None:
        self.kind = kind
        # Public text is always catalog-backed. Caller ``message`` is ignored so
        # tool stderr/URLs cannot inject into str()/logs.
        del message
        self.message = _PUBLIC_MESSAGES[kind]
        self.internal_reason = sanitize_internal_reason(internal_reason)
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.message}"

    def __repr__(self) -> str:
        # Never include internal_reason — it must not appear in logs via %r.
        return f"InspectionError(kind={self.kind!r}, message={self.message!r})"


def raise_inspection_error(
    kind: InspectionErrorKind,
    *,
    message: str | None = None,
    internal_reason: str | None = None,
) -> NoReturn:
    """Raise a typed inspection error with a catalog public message."""
    del message
    raise InspectionError(kind, internal_reason=internal_reason)
