"""Typed download-job domain errors (FastAPI-independent)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import NoReturn

# Process-local diagnostics only. Arbitrary text is rejected so secrets never
# linger on the exception object for logging or repr.
_SAFE_INTERNAL_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class DownloadErrorCode(StrEnum):
    """Public uppercase API/download error codes for download orchestration."""

    DOWNLOADS_DISABLED = "DOWNLOADS_DISABLED"
    DOWNLOAD_JOB_NOT_FOUND = "DOWNLOAD_JOB_NOT_FOUND"
    PARENT_JOB_NOT_READY = "PARENT_JOB_NOT_READY"
    FORMAT_NOT_FOUND = "FORMAT_NOT_FOUND"
    FORMAT_NOT_ELIGIBLE = "FORMAT_NOT_ELIGIBLE"
    FORMAT_UNAVAILABLE = "FORMAT_UNAVAILABLE"
    DOWNLOAD_TIMEOUT = "DOWNLOAD_TIMEOUT"
    DOWNLOAD_TOO_LARGE = "DOWNLOAD_TOO_LARGE"
    DOWNLOAD_TOOL_FAILED = "DOWNLOAD_TOOL_FAILED"
    DOWNLOAD_INVALID_OUTPUT = "DOWNLOAD_INVALID_OUTPUT"
    DOWNLOAD_STORAGE_UNAVAILABLE = "DOWNLOAD_STORAGE_UNAVAILABLE"
    DOWNLOAD_EXPIRED = "DOWNLOAD_EXPIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_PUBLIC_MESSAGES: dict[DownloadErrorCode, str] = {
    DownloadErrorCode.DOWNLOADS_DISABLED: "Downloads are not available.",
    DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND: "Download job not found.",
    DownloadErrorCode.PARENT_JOB_NOT_READY: "The media job is not ready for download.",
    DownloadErrorCode.FORMAT_NOT_FOUND: "The requested format was not found.",
    DownloadErrorCode.FORMAT_NOT_ELIGIBLE: "The requested format is not eligible.",
    DownloadErrorCode.FORMAT_UNAVAILABLE: (
        "The requested format is no longer available."
    ),
    DownloadErrorCode.DOWNLOAD_TIMEOUT: "The download timed out.",
    DownloadErrorCode.DOWNLOAD_TOO_LARGE: "The download exceeds the allowed size.",
    DownloadErrorCode.DOWNLOAD_TOOL_FAILED: "The download tool failed.",
    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT: "The download produced invalid output.",
    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE: (
        "Download storage is temporarily unavailable."
    ),
    DownloadErrorCode.DOWNLOAD_EXPIRED: "The download job or artifact expired.",
    DownloadErrorCode.INTERNAL_ERROR: "An unexpected error occurred.",
}

_HTTP_STATUS: dict[DownloadErrorCode, int] = {
    DownloadErrorCode.DOWNLOADS_DISABLED: 503,
    DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND: 404,
    DownloadErrorCode.PARENT_JOB_NOT_READY: 409,
    DownloadErrorCode.FORMAT_NOT_FOUND: 404,
    DownloadErrorCode.FORMAT_NOT_ELIGIBLE: 422,
    DownloadErrorCode.FORMAT_UNAVAILABLE: 422,
    DownloadErrorCode.DOWNLOAD_TIMEOUT: 504,
    DownloadErrorCode.DOWNLOAD_TOO_LARGE: 413,
    DownloadErrorCode.DOWNLOAD_TOOL_FAILED: 422,
    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT: 422,
    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE: 503,
    DownloadErrorCode.DOWNLOAD_EXPIRED: 410,
    DownloadErrorCode.INTERNAL_ERROR: 500,
}


def sanitize_internal_reason(raw: str | None) -> str | None:
    """Accept only short uppercase diagnostic codes; drop everything else."""
    if raw is None:
        return None
    if _SAFE_INTERNAL_REASON.fullmatch(raw):
        return raw
    return "REDACTED_INTERNAL_REASON"


class DownloadError(Exception):
    """Fail-closed download failure with a stable public-safe message."""

    def __init__(
        self,
        code: DownloadErrorCode,
        *,
        message: str | None = None,
        internal_reason: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.kind = code
        # Public text is always catalog-backed. Caller ``message`` is ignored so
        # tokens/URLs/paths cannot inject into str()/logs.
        del message
        self.message = _PUBLIC_MESSAGES[code]
        self.http_status = (
            http_status if http_status is not None else _HTTP_STATUS[code]
        )
        self.internal_reason = sanitize_internal_reason(internal_reason)
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def __repr__(self) -> str:
        # Never include internal_reason or any token/path material.
        return (
            f"DownloadError(code={self.code!r}, message={self.message!r}, "
            f"http_status={self.http_status!r})"
        )


def raise_download_error(
    code: DownloadErrorCode,
    *,
    message: str | None = None,
    internal_reason: str | None = None,
    http_status: int | None = None,
) -> NoReturn:
    """Raise a typed download error with a catalog public message."""
    del message
    raise DownloadError(
        code,
        internal_reason=internal_reason,
        http_status=http_status,
    )


def public_message_for(code: DownloadErrorCode) -> str:
    """Return the catalog public message for a download error code."""
    return _PUBLIC_MESSAGES[code]


def http_status_for(code: DownloadErrorCode) -> int:
    """Return the catalog HTTP status for a download error code."""
    return _HTTP_STATUS[code]
