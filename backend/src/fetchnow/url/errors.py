"""Typed URL / network errors mapped to public API codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn


@dataclass(frozen=True, slots=True)
class URLValidationError(Exception):
    """Domain validation / outbound failure with a stable public error code."""

    code: str
    message: str
    # Machine-readable internal detail; never copied into public API messages.
    internal_reason: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


# Alias used by the network layer for clarity.
NetworkError = URLValidationError


PUBLIC_MESSAGES: dict[str, str] = {
    "INVALID_URL": "The provided URL could not be accepted.",
    "UNSUPPORTED_SCHEME": "Only http and https URLs are supported.",
    "UNSUPPORTED_PROVIDER": "This media provider is not supported.",
    "BLOCKED_DESTINATION": "The destination is not allowed.",
    "REDIRECT_LIMIT_EXCEEDED": "Too many redirects were encountered.",
    "INVALID_REDIRECT": "The redirect target could not be accepted.",
    "SOURCE_UNAVAILABLE": "The media source could not be reached.",
    "SOURCE_TIMEOUT": "Looking up or contacting the media host timed out.",
    "RESPONSE_TOO_LARGE": "The response exceeded the allowed size.",
    "UNSUPPORTED_CONTENT_TYPE": "The response content type is not allowed.",
    "INTERNAL_ERROR": "An unexpected error occurred.",
}

HTTP_STATUS_BY_CODE: dict[str, int] = {
    "INVALID_URL": 400,
    "UNSUPPORTED_SCHEME": 400,
    "UNSUPPORTED_PROVIDER": 422,
    "BLOCKED_DESTINATION": 422,
    "REDIRECT_LIMIT_EXCEEDED": 422,
    "INVALID_REDIRECT": 422,
    "UNSUPPORTED_CONTENT_TYPE": 422,
    "RESPONSE_TOO_LARGE": 413,
    "SOURCE_UNAVAILABLE": 502,
    "SOURCE_TIMEOUT": 504,
    "INTERNAL_ERROR": 500,
}


def raise_validation_error(
    code: str,
    *,
    message: str | None = None,
    internal_reason: str | None = None,
) -> NoReturn:
    """Raise a domain error with a public-safe message."""
    raise URLValidationError(
        code=code,
        message=message or PUBLIC_MESSAGES.get(code, PUBLIC_MESSAGES["INTERNAL_ERROR"]),
        internal_reason=internal_reason,
    )


raise_network_error = raise_validation_error
