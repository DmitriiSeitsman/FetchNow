"""Map resolution domain outcomes to public API codes (PR3B)."""

from __future__ import annotations

from fetchnow.resolution.errors import ResolutionError, ResolutionErrorKind

# Uppercase public API codes (stable client contract).
RESOLUTION_PUBLIC_CODES: dict[ResolutionErrorKind, str] = {
    ResolutionErrorKind.WRAPPER_UNSUPPORTED: "WRAPPER_UNSUPPORTED",
    ResolutionErrorKind.WRAPPER_UNRESOLVED: "WRAPPER_UNRESOLVED",
    ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED: "RESOLVED_PROVIDER_UNSUPPORTED",
    ResolutionErrorKind.RESOLUTION_LOOP: "RESOLUTION_LOOP",
    ResolutionErrorKind.RESOLUTION_LIMIT_EXCEEDED: "RESOLUTION_LIMIT_EXCEEDED",
    ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET: "UNSAFE_RESOLUTION_TARGET",
    ResolutionErrorKind.INTERNAL_ERROR: "INTERNAL_ERROR",
}

RESOLUTION_HTTP_STATUS: dict[str, int] = {
    "WRAPPER_UNSUPPORTED": 422,
    "WRAPPER_UNRESOLVED": 422,
    "RESOLVED_PROVIDER_UNSUPPORTED": 422,
    "RESOLUTION_LOOP": 422,
    "RESOLUTION_LIMIT_EXCEEDED": 422,
    "UNSAFE_RESOLUTION_TARGET": 422,
    "INTERNAL_ERROR": 500,
}

RESOLUTION_PUBLIC_MESSAGES: dict[str, str] = {
    "WRAPPER_UNSUPPORTED": "This URL wrapper is not supported.",
    "WRAPPER_UNRESOLVED": "The wrapper could not be resolved.",
    "RESOLVED_PROVIDER_UNSUPPORTED": (
        "The resolved media destination is not supported."
    ),
    "RESOLUTION_LOOP": "Wrapper resolution detected a loop.",
    "RESOLUTION_LIMIT_EXCEEDED": "Wrapper resolution exceeded the allowed depth.",
    "UNSAFE_RESOLUTION_TARGET": "The resolution target is not allowed.",
    "INTERNAL_ERROR": "An unexpected resolution error occurred.",
}


def map_resolution_error(exc: ResolutionError) -> tuple[str, int, str]:
    """Return ``(public_code, http_status, public_message)`` for an API response."""
    code = RESOLUTION_PUBLIC_CODES.get(exc.kind, "INTERNAL_ERROR")
    status = RESOLUTION_HTTP_STATUS.get(code, 500)
    message = RESOLUTION_PUBLIC_MESSAGES.get(
        code, RESOLUTION_PUBLIC_MESSAGES["INTERNAL_ERROR"]
    )
    return code, status, message
