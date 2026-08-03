"""Typed URL validation errors mapped to public API codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn


@dataclass(frozen=True, slots=True)
class URLValidationError(Exception):
    """Domain validation failure with a stable public error code."""

    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


PUBLIC_MESSAGES: dict[str, str] = {
    "INVALID_URL": "The provided URL could not be accepted.",
    "UNSUPPORTED_SCHEME": "Only http and https URLs are supported.",
    "UNSUPPORTED_PROVIDER": "This media provider is not supported.",
    "BLOCKED_DESTINATION": "The destination is not allowed.",
    "SOURCE_TIMEOUT": "Looking up the media host timed out.",
    "INTERNAL_ERROR": "An unexpected error occurred.",
}

HTTP_STATUS_BY_CODE: dict[str, int] = {
    "INVALID_URL": 400,
    "UNSUPPORTED_SCHEME": 400,
    "UNSUPPORTED_PROVIDER": 422,
    "BLOCKED_DESTINATION": 422,
    "SOURCE_TIMEOUT": 504,
    "INTERNAL_ERROR": 500,
}


def raise_validation_error(code: str, *, message: str | None = None) -> NoReturn:
    """Raise a domain error with a public-safe message."""
    raise URLValidationError(
        code=code,
        message=message or PUBLIC_MESSAGES.get(code, PUBLIC_MESSAGES["INTERNAL_ERROR"]),
    )
