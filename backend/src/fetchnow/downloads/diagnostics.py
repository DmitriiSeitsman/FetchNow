"""Allowlisted download-stage diagnostics. Never log secrets, URLs, or tool text."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from fetchnow.downloads.errors import DownloadErrorCode

_SAFE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_STDERR_CLASSIFY_BYTES = 65_536

logger = logging.getLogger("fetchnow.downloads.diagnostics")


class FailureClass(StrEnum):
    """Internal operational failure class. Never returned on the public API."""

    TOOL_EXIT_NONZERO = "TOOL_EXIT_NONZERO"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_SIGNALLED = "TOOL_SIGNALLED"
    OUTPUT_MISSING = "OUTPUT_MISSING"
    FORMAT_UNAVAILABLE = "FORMAT_UNAVAILABLE"
    HTTP_AUTH_REQUIRED = "HTTP_AUTH_REQUIRED"
    HTTP_FORBIDDEN = "HTTP_FORBIDDEN"
    HTTP_NOT_FOUND = "HTTP_NOT_FOUND"
    NETWORK_DNS_FAILED = "NETWORK_DNS_FAILED"
    NETWORK_CONNECT_FAILED = "NETWORK_CONNECT_FAILED"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    TLS_FAILED = "TLS_FAILED"
    GEO_RESTRICTED = "GEO_RESTRICTED"
    MEDIA_TRANSFER_FAILED = "MEDIA_TRANSFER_FAILED"
    PROVIDER_RESPONSE_CHANGED = "PROVIDER_RESPONSE_CHANGED"
    MUX_FAILED = "MUX_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    LEASE_LOST = "LEASE_LOST"
    CANCELLED = "CANCELLED"


class ProcessExitCategory(StrEnum):
    """Coarse subprocess exit category. No signal numbers or argv."""

    ZERO = "ZERO"
    NONZERO = "NONZERO"
    TIMEOUT = "TIMEOUT"
    SIGNALLED = "SIGNALLED"
    CANCELLED = "CANCELLED"


class DownloadStageEvent(StrEnum):
    """Structured stage event names (log message allowlist)."""

    ATTEMPT_STARTED = "download_job_attempt_started"
    VIDEO_STARTED = "download_video_started"
    VIDEO_COMPLETED = "download_video_completed"
    VIDEO_FAILED = "download_video_failed"
    AUDIO_STARTED = "download_audio_started"
    AUDIO_COMPLETED = "download_audio_completed"
    AUDIO_FAILED = "download_audio_failed"
    MUX_STARTED = "media_mux_started"
    MUX_COMPLETED = "media_mux_completed"
    MUX_FAILED = "media_mux_failed"
    VERIFY_STARTED = "media_verify_started"
    VERIFY_COMPLETED = "media_verify_completed"
    VERIFY_FAILED = "media_verify_failed"
    PUBLISH_STARTED = "artifact_publish_started"
    PUBLISH_COMPLETED = "artifact_publish_completed"
    PUBLISH_FAILED = "artifact_publish_failed"
    RETRY_SCHEDULED = "download_retry_scheduled"
    JOB_CANCELLED = "download_job_cancelled"
    JOB_FAILED = "download_job_failed"
    JOB_READY = "download_job_ready"


_EVENT_NAMES = frozenset(item.value for item in DownloadStageEvent)
_FAILURE_CLASSES = frozenset(item.value for item in FailureClass)
_EXIT_CATEGORIES = frozenset(item.value for item in ProcessExitCategory)
_ERROR_CODES = frozenset(item.value for item in DownloadErrorCode)
_STAGE_TOKENS = frozenset(
    {
        "queued",
        "inspecting",
        "downloading_video",
        "downloading_audio",
        "muxing",
        "verifying",
        "publishing",
        "retrying",
        "ready",
        "failed",
        "cancelled",
        "expired",
        "video",
        "audio",
        "mux",
        "verify",
        "publish",
        "attempt",
    }
)

_ALLOWED_EXTRA_KEYS = frozenset(
    {
        "download_job_id",
        "media_job_id",
        "attempt_count",
        "fence_token",
        "stage",
        "duration_ms",
        "retryable",
        "error_code",
        "failure_class",
        "process_exit_category",
        "stdout_byte_count",
        "stderr_byte_count",
        "diagnostic_fingerprint",
    }
)

_URLISH = re.compile(
    rb"https?://|www\.|vk\.com|rutube\.ru|ok\.ru|odnoklassniki\.ru|dzen\.ru|"
    rb"zen\.yandex\.|yandex\.|token=|/tmp/|/var/"
)


def process_exit_category(
    *,
    exit_code: int | None,
    timed_out: bool,
    signal: int | None,
    cancelled: bool,
) -> ProcessExitCategory:
    """Map process outcome to a coarse category. Never includes the raw code."""
    if cancelled:
        return ProcessExitCategory.CANCELLED
    if timed_out:
        return ProcessExitCategory.TIMEOUT
    if signal is not None:
        return ProcessExitCategory.SIGNALLED
    if exit_code == 0:
        return ProcessExitCategory.ZERO
    return ProcessExitCategory.NONZERO


def classify_tool_failure(
    *,
    timed_out: bool = False,
    signal: int | None = None,
    cancelled: bool = False,
    stderr: bytes = b"",
    default: FailureClass = FailureClass.TOOL_EXIT_NONZERO,
) -> FailureClass:
    """Classify a tool failure from bounded stderr. Never raises. Never logs stderr.

    Unknown or unreadable text maps to ``TOOL_EXIT_NONZERO``. Secrets and URLs
    are not copied into the returned class.
    """
    try:
        if cancelled:
            return FailureClass.CANCELLED
        if timed_out:
            return FailureClass.TOOL_TIMEOUT
        if signal is not None:
            return FailureClass.TOOL_SIGNALLED
        blob = stderr[:_MAX_STDERR_CLASSIFY_BYTES]
        lowered = blob.lower()
        if b"http error 401" in lowered or b"unauthorized" in lowered:
            return FailureClass.HTTP_AUTH_REQUIRED
        if b"http error 403" in lowered or b"forbidden" in lowered:
            return FailureClass.HTTP_FORBIDDEN
        if b"http error 404" in lowered or b"not found" in lowered:
            return FailureClass.HTTP_NOT_FOUND
        if any(
            marker in lowered
            for marker in (
                b"temporary failure in name resolution",
                b"name or service not known",
                b"nodename nor servname provided",
                b"getaddrinfo failed",
                b"no address associated with hostname",
            )
        ):
            return FailureClass.NETWORK_DNS_FAILED
        if any(
            marker in lowered
            for marker in (
                b"certificate verify failed",
                b"tlsv1 alert",
                b"ssl: certificate",
                b"ssl certificate",
            )
        ):
            return FailureClass.TLS_FAILED
        if any(
            marker in lowered
            for marker in (
                b"not available in your country",
                b"not available from your location",
                b"geo-restricted",
                b"georestricted",
            )
        ):
            return FailureClass.GEO_RESTRICTED
        if any(
            marker in lowered
            for marker in (
                b"connection timed out",
                b"read operation timed out",
                b"the read operation timed out",
            )
        ):
            return FailureClass.NETWORK_TIMEOUT
        if any(
            marker in lowered
            for marker in (
                b"connection refused",
                b"network is unreachable",
                b"no route to host",
                b"remote end closed connection",
                b"connection reset by peer",
            )
        ):
            return FailureClass.NETWORK_CONNECT_FAILED
        if (
            b"requested format is not available" in lowered
            or b"requested format not available" in lowered
        ):
            return FailureClass.FORMAT_UNAVAILABLE
        if b"unable to extract" in lowered or b"no video formats found" in lowered:
            return FailureClass.PROVIDER_RESPONSE_CHANGED
        if any(
            marker in lowered
            for marker in (
                b"unable to download video data",
                b"unable to download webpage",
                b"fragment not found",
                b"unable to continue",
            )
        ):
            return FailureClass.MEDIA_TRANSFER_FAILED
        return default
    except Exception:
        return FailureClass.TOOL_EXIT_NONZERO


def diagnostic_fingerprint(
    *,
    stage: str,
    failure_class: str | None,
    exit_category: str | None,
    retryable: bool | None,
    error_code: str | None,
) -> str:
    """Stable fingerprint over sanitized categorical fields only."""
    retry_token = (
        "retryable"
        if retryable is True
        else "not_retryable"
        if retryable is False
        else "na"
    )
    parts = [
        "v1",
        _safe_token(stage, fallback="unknown"),
        _safe_token(failure_class, fallback="none"),
        _safe_token(exit_category, fallback="none"),
        retry_token,
        _safe_token(error_code, fallback="none"),
    ]
    material = "|".join(parts).encode("ascii")
    digest = hashlib.sha256(b"fetchnow-diag-fp-v1\0" + material).hexdigest()
    return digest[:16]


def emit_stage_event(
    event: DownloadStageEvent | str,
    *,
    download_job_id: str | None = None,
    media_job_id: str | None = None,
    attempt_count: int | None = None,
    fence_token: int | None = None,
    stage: str | None = None,
    duration_ms: int | None = None,
    retryable: bool | None = None,
    error_code: str | None = None,
    failure_class: str | FailureClass | None = None,
    process_exit_category: str | ProcessExitCategory | None = None,
    stdout_byte_count: int | None = None,
    stderr_byte_count: int | None = None,
    diagnostic_fingerprint_value: str | None = None,
) -> None:
    """Emit one allowlisted stage event. Extra keys and unsafe values are dropped."""
    name = event.value if isinstance(event, DownloadStageEvent) else str(event)
    if name not in _EVENT_NAMES:
        name = DownloadStageEvent.JOB_FAILED.value
    extra: dict[str, Any] = {}
    if download_job_id and _SAFE_ID.fullmatch(download_job_id):
        extra["download_job_id"] = download_job_id.lower()
    if media_job_id and _SAFE_ID.fullmatch(media_job_id):
        extra["media_job_id"] = media_job_id.lower()
    if isinstance(attempt_count, int) and not isinstance(attempt_count, bool):
        extra["attempt_count"] = max(0, min(attempt_count, 1_000_000))
    if isinstance(fence_token, int) and not isinstance(fence_token, bool):
        extra["fence_token"] = max(0, min(fence_token, 10**12))
    if stage in _STAGE_TOKENS:
        extra["stage"] = stage
    if (
        isinstance(duration_ms, int)
        and not isinstance(duration_ms, bool)
        and duration_ms >= 0
    ):
        extra["duration_ms"] = min(duration_ms, 86_400_000)
    if retryable is True or retryable is False:
        extra["retryable"] = retryable
    if error_code in _ERROR_CODES:
        extra["error_code"] = error_code
    fc = (
        failure_class.value
        if isinstance(failure_class, FailureClass)
        else failure_class
    )
    if fc in _FAILURE_CLASSES:
        extra["failure_class"] = fc
    pec = (
        process_exit_category.value
        if isinstance(process_exit_category, ProcessExitCategory)
        else process_exit_category
    )
    if pec in _EXIT_CATEGORIES:
        extra["process_exit_category"] = pec
    if isinstance(stdout_byte_count, int) and not isinstance(stdout_byte_count, bool):
        extra["stdout_byte_count"] = max(0, min(stdout_byte_count, 10**12))
    if isinstance(stderr_byte_count, int) and not isinstance(stderr_byte_count, bool):
        extra["stderr_byte_count"] = max(0, min(stderr_byte_count, 10**12))
    fp = diagnostic_fingerprint_value
    if fp is None and (fc or pec or error_code or stage):
        fp = diagnostic_fingerprint(
            stage=str(extra.get("stage") or "unknown"),
            failure_class=fc if isinstance(fc, str) else None,
            exit_category=pec if isinstance(pec, str) else None,
            retryable=retryable,
            error_code=error_code if isinstance(error_code, str) else None,
        )
    if isinstance(fp, str) and re.fullmatch(r"^[0-9a-f]{8,32}$", fp):
        extra["diagnostic_fingerprint"] = fp
    logger.info(name, extra=extra)


def assert_log_record_safe(record: Mapping[str, Any] | str) -> None:
    """Adversarial helper: fail tests when forbidden material is present."""
    blob = str(record).lower()
    encoded = blob.encode("utf-8", "surrogateescape")
    forbidden = (
        "http://",
        "https://",
        "bearer ",
        "provider_format_token",
        "format_id",
        "/tmp/",
        "/var/lib/",
    )
    if _URLISH.search(encoded) and any(token in blob for token in forbidden):
        raise AssertionError("unsafe diagnostic material")


def _safe_token(raw: str | None, *, fallback: str) -> str:
    if raw and _SAFE_TOKEN.fullmatch(raw):
        return raw
    return fallback
