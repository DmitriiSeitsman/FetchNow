"""Typed wrapper-resolution domain errors (FastAPI-independent)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import NoReturn

# Internal reasons are process-local diagnostics only. Arbitrary resolver text
# is rejected so secrets never linger on the exception object for logging.
_SAFE_INTERNAL_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ResolutionErrorKind(StrEnum):
    """Lower-case domain outcomes for wrapper resolution.

    These are not public API error codes. API mapping is deferred to the
    first production resolver wiring (PR3B+). Existing ``/media/validate``
    and ``/media/probe`` routes keep their uppercase URL/network codes.
    """

    WRAPPER_UNSUPPORTED = "wrapper_unsupported"
    WRAPPER_UNRESOLVED = "wrapper_unresolved"
    RESOLVED_PROVIDER_UNSUPPORTED = "resolved_provider_unsupported"
    RESOLUTION_LOOP = "resolution_loop"
    RESOLUTION_LIMIT_EXCEEDED = "resolution_limit_exceeded"
    UNSAFE_RESOLUTION_TARGET = "unsafe_resolution_target"
    INTERNAL_ERROR = "internal_error"


_PUBLIC_MESSAGES: dict[ResolutionErrorKind, str] = {
    ResolutionErrorKind.WRAPPER_UNSUPPORTED: "This URL wrapper is not supported.",
    ResolutionErrorKind.WRAPPER_UNRESOLVED: "The wrapper could not be resolved.",
    ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED: (
        "The resolved media destination is not supported."
    ),
    ResolutionErrorKind.RESOLUTION_LOOP: "Wrapper resolution detected a loop.",
    ResolutionErrorKind.RESOLUTION_LIMIT_EXCEEDED: (
        "Wrapper resolution exceeded the allowed depth."
    ),
    ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET: (
        "The resolution target is not allowed."
    ),
    ResolutionErrorKind.INTERNAL_ERROR: "An unexpected resolution error occurred.",
}


def sanitize_internal_reason(raw: str | None) -> str | None:
    """Accept only short uppercase diagnostic codes; drop everything else."""
    if raw is None:
        return None
    if _SAFE_INTERNAL_REASON.fullmatch(raw):
        return raw
    return "REDACTED_INTERNAL_REASON"


class ResolutionError(Exception):
    """Fail-closed resolution failure with a stable public-safe message."""

    def __init__(
        self,
        kind: ResolutionErrorKind,
        *,
        message: str | None = None,
        internal_reason: str | None = None,
    ) -> None:
        self.kind = kind
        # Public text is always catalog-backed. Caller ``message`` is ignored so
        # a malicious resolver cannot inject URLs/tokens into str()/logs.
        del message
        self.message = _PUBLIC_MESSAGES[kind]
        self.internal_reason = sanitize_internal_reason(internal_reason)
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.message}"

    def __repr__(self) -> str:
        # Never include internal_reason — it must not appear in logs via %r.
        return f"ResolutionError(kind={self.kind!r}, message={self.message!r})"


def raise_resolution_error(
    kind: ResolutionErrorKind,
    *,
    message: str | None = None,
    internal_reason: str | None = None,
) -> NoReturn:
    """Raise a typed resolution error with a catalog public message."""
    del message
    raise ResolutionError(kind, internal_reason=internal_reason)


def normalize_resolver_error(exc: ResolutionError) -> ResolutionError:
    """Rebuild a resolver-raised error with catalog message and safe reason."""
    return ResolutionError(exc.kind, internal_reason=exc.internal_reason)
