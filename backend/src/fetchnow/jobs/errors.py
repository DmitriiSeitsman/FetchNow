"""Typed media-job domain errors (FastAPI-independent)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import NoReturn

# Process-local diagnostics only. Arbitrary text is rejected so secrets never
# linger on the exception object for logging or repr.
_SAFE_INTERNAL_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class JobErrorCode(StrEnum):
    """Public uppercase API/job error codes for media-job orchestration."""

    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_EXPIRED = "JOB_EXPIRED"
    CAPACITY_UNAVAILABLE = "CAPACITY_UNAVAILABLE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_ACCESS_TOKEN = "INVALID_ACCESS_TOKEN"
    MEDIA_INSPECTION_FAILED = "MEDIA_INSPECTION_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_PUBLIC_MESSAGES: dict[JobErrorCode, str] = {
    JobErrorCode.JOB_NOT_FOUND: "Job not found.",
    JobErrorCode.JOB_EXPIRED: "Job or file expired.",
    JobErrorCode.CAPACITY_UNAVAILABLE: "Service is temporarily at capacity.",
    JobErrorCode.IDEMPOTENCY_CONFLICT: (
        "The access token was already used for a different request."
    ),
    JobErrorCode.INVALID_ACCESS_TOKEN: "The access token is invalid.",
    JobErrorCode.MEDIA_INSPECTION_FAILED: "Media inspection failed.",
    JobErrorCode.INTERNAL_ERROR: "An unexpected error occurred.",
}

_HTTP_STATUS: dict[JobErrorCode, int] = {
    JobErrorCode.JOB_NOT_FOUND: 404,
    JobErrorCode.JOB_EXPIRED: 410,
    JobErrorCode.CAPACITY_UNAVAILABLE: 503,
    JobErrorCode.IDEMPOTENCY_CONFLICT: 409,
    JobErrorCode.INVALID_ACCESS_TOKEN: 400,
    JobErrorCode.MEDIA_INSPECTION_FAILED: 422,
    JobErrorCode.INTERNAL_ERROR: 500,
}


def sanitize_internal_reason(raw: str | None) -> str | None:
    """Accept only short uppercase diagnostic codes; drop everything else."""
    if raw is None:
        return None
    if _SAFE_INTERNAL_REASON.fullmatch(raw):
        return raw
    return "REDACTED_INTERNAL_REASON"


class JobError(Exception):
    """Fail-closed job failure with a stable public-safe message."""

    def __init__(
        self,
        code: JobErrorCode,
        *,
        message: str | None = None,
        internal_reason: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.kind = code
        # Public text is always catalog-backed. Caller ``message`` is ignored so
        # tokens/URLs cannot inject into str()/logs.
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
        # Never include internal_reason or any token material.
        return (
            f"JobError(code={self.code!r}, message={self.message!r}, "
            f"http_status={self.http_status!r})"
        )


def raise_job_error(
    code: JobErrorCode,
    *,
    message: str | None = None,
    internal_reason: str | None = None,
    http_status: int | None = None,
) -> NoReturn:
    """Raise a typed job error with a catalog public message."""
    del message
    raise JobError(
        code,
        internal_reason=internal_reason,
        http_status=http_status,
    )


def public_message_for(code: JobErrorCode) -> str:
    """Return the catalog public message for a job error code."""
    return _PUBLIC_MESSAGES[code]


def http_status_for(code: JobErrorCode) -> int:
    """Return the catalog HTTP status for a job error code."""
    return _HTTP_STATUS[code]
