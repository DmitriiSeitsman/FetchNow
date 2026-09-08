"""Structured JSON logging for FetchNow processes."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

# Extra LogRecord attributes that are safe to emit when present.
_SAFE_EXTRA_FIELDS = (
    "request_id",
    "outcome",
    "provider_id",
    "hostname",
    "error_code",
    "duration_ms",
    "route",
    "redirect_count",
    "body_bytes_read",
    "http_method",
    "http_status",
    "internal_reason",
)

# These fields are safe only after the download diagnostics emitter has
# validated them. Keep this boundary logger-specific so an unrelated logger
# cannot smuggle arbitrary strings into structured production output merely by
# choosing one of the diagnostic field names.
_DOWNLOAD_DIAGNOSTICS_LOGGER = "fetchnow.downloads.diagnostics"
_DOWNLOAD_DIAGNOSTIC_FIELDS = (
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
)
_SAFE_DIAGNOSTIC_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SAFE_DIAGNOSTIC_FINGERPRINT = re.compile(r"^[0-9a-f]{8,32}$")
_SAFE_DOWNLOAD_STAGES = frozenset(
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
_SAFE_DOWNLOAD_ERROR_CODES = frozenset(
    {
        "DOWNLOADS_DISABLED",
        "DOWNLOAD_JOB_NOT_FOUND",
        "PARENT_JOB_NOT_READY",
        "FORMAT_NOT_FOUND",
        "FORMAT_NOT_ELIGIBLE",
        "FORMAT_UNAVAILABLE",
        "DOWNLOAD_TIMEOUT",
        "DOWNLOAD_TOO_LARGE",
        "DOWNLOAD_TOOL_FAILED",
        "DOWNLOAD_INVALID_OUTPUT",
        "DOWNLOAD_STORAGE_UNAVAILABLE",
        "DOWNLOAD_EXPIRED",
        "DELIVERY_DISABLED",
        "DOWNLOAD_NOT_READY",
        "MUXING_UNAVAILABLE",
        "MUXING_FAILED",
        "MUXING_TIMEOUT",
        "MUXED_OUTPUT_INVALID",
        "INTERNAL_ERROR",
    }
)
_SAFE_DOWNLOAD_FAILURE_CLASSES = frozenset(
    {
        "TOOL_EXIT_NONZERO",
        "TOOL_TIMEOUT",
        "TOOL_SIGNALLED",
        "OUTPUT_MISSING",
        "FORMAT_UNAVAILABLE",
        "HTTP_AUTH_REQUIRED",
        "HTTP_FORBIDDEN",
        "HTTP_NOT_FOUND",
        "NETWORK_DNS_FAILED",
        "NETWORK_CONNECT_FAILED",
        "NETWORK_TIMEOUT",
        "TLS_FAILED",
        "GEO_RESTRICTED",
        "MEDIA_TRANSFER_FAILED",
        "PROVIDER_RESPONSE_CHANGED",
        "MUX_FAILED",
        "VERIFY_FAILED",
        "LEASE_LOST",
        "CANCELLED",
    }
)
_SAFE_PROCESS_EXIT_CATEGORIES = frozenset(
    {"ZERO", "NONZERO", "TIMEOUT", "SIGNALLED", "CANCELLED"}
)

_PAYMENT_AUDIT_LOGGER = "fetchnow.payments.audit"
_PAYMENT_AUDIT_FIELDS = (
    "payment_order_fingerprint",
    "provider_invoice_id",
    "previous_status",
    "new_status",
    "signature_valid",
    "amount_match",
    "is_test",
)
_SAFE_PAYMENT_FINGERPRINT = re.compile(r"^[0-9a-f]{16}$")
_SAFE_PAYMENT_STATES = frozenset({"created", "pending", "paid", "expired"})

_PAYMENT_CALLBACK_DIAGNOSTIC_LOGGER = "fetchnow.payments.callback_diagnostic"
_PAYMENT_CALLBACK_DIAGNOSTIC_FIELDS = (
    "content_type_category",
    "callback_field_names",
    "expected_fields_present",
    "unexpected_callback_field_names",
    "parser_entered",
    "parser_accepted",
    "inv_id_parsing_attempted",
    "inv_id_valid",
    "signature_verification_attempted",
    "signature_valid",
    "amount_validation_attempted",
    "amount_valid",
    "order_lookup_succeeded",
    "receipt_validation_attempted",
    "rejection_stage",
    "rejection_category",
    "response_outcome",
)
_SAFE_CALLBACK_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_CALLBACK_CONTENT_TYPES = frozenset(
    {"form_urlencoded", "missing", "other"}
)
_SAFE_CALLBACK_STAGES = frozenset(
    {
        "none",
        "content_type",
        "form_decode",
        "field_schema",
        "inv_id",
        "order_lookup",
        "signature",
        "amount",
        "snapshot",
        "persistence",
    }
)
_SAFE_CALLBACK_CATEGORIES = frozenset(
    {
        "none",
        "content_type_invalid",
        "form_invalid",
        "required_field_missing",
        "duplicate_field",
        "unexpected_field",
        "inv_id_invalid",
        "order_not_found",
        "signature_invalid",
        "amount_invalid",
        "snapshot_invalid",
        "payments_disabled",
        "internal_error",
    }
)


def _safe_payment_audit_value(field: str, value: Any) -> Any | None:
    if field == "payment_order_fingerprint":
        if isinstance(value, str) and _SAFE_PAYMENT_FINGERPRINT.fullmatch(value):
            return value
        return None
    if field == "provider_invoice_id":
        return (
            value
            if type(value) is int and 1 <= value <= 9_223_372_036_854_775_807
            else None
        )
    if field in {"previous_status", "new_status"}:
        return value if value in _SAFE_PAYMENT_STATES else None
    if field in {"signature_valid", "amount_match", "is_test"}:
        return value if type(value) is bool else None
    return None


def _safe_callback_diagnostic_value(field: str, value: Any) -> Any | None:
    if field == "content_type_category":
        return value if value in _SAFE_CALLBACK_CONTENT_TYPES else None
    if field in {"callback_field_names", "unexpected_callback_field_names"}:
        if not isinstance(value, tuple | list) or len(value) > 16:
            return None
        if not all(
            isinstance(item, str) and _SAFE_CALLBACK_FIELD_NAME.fullmatch(item)
            for item in value
        ):
            return None
        return list(value)
    if field in {
        "expected_fields_present",
        "parser_entered",
        "parser_accepted",
        "inv_id_parsing_attempted",
        "inv_id_valid",
        "signature_verification_attempted",
        "signature_valid",
        "amount_validation_attempted",
        "amount_valid",
        "receipt_validation_attempted",
    }:
        return value if type(value) is bool else None
    if field == "order_lookup_succeeded":
        return value if value is None or type(value) is bool else None
    if field == "rejection_stage":
        return value if value in _SAFE_CALLBACK_STAGES else None
    if field == "rejection_category":
        return value if value in _SAFE_CALLBACK_CATEGORIES else None
    if field == "response_outcome":
        return value if value in {"OK", "ERROR"} else None
    return None


def _safe_download_diagnostic_value(field: str, value: Any) -> Any | None:
    """Validate diagnostic extras again at the final serialization boundary."""
    if field in {"download_job_id", "media_job_id"}:
        if isinstance(value, str) and _SAFE_DIAGNOSTIC_ID.fullmatch(value):
            return value.lower()
        return None
    if field in {
        "attempt_count",
        "fence_token",
        "stdout_byte_count",
        "stderr_byte_count",
    }:
        return value if type(value) is int and 0 <= value <= 10**12 else None
    if field == "duration_ms":
        return value if type(value) is int and 0 <= value <= 86_400_000 else None
    if field == "retryable":
        return value if type(value) is bool else None
    if field == "stage":
        return value if value in _SAFE_DOWNLOAD_STAGES else None
    if field == "error_code":
        return value if value in _SAFE_DOWNLOAD_ERROR_CODES else None
    if field == "failure_class":
        return value if value in _SAFE_DOWNLOAD_FAILURE_CLASSES else None
    if field == "process_exit_category":
        return value if value in _SAFE_PROCESS_EXIT_CATEGORIES else None
    if field == "diagnostic_fingerprint":
        if (
            isinstance(value, str)
            and _SAFE_DIAGNOSTIC_FINGERPRINT.fullmatch(value)
        ):
            return value
        return None
    return None


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _SAFE_EXTRA_FIELDS:
            value = getattr(record, field, None)
            if record.name == _DOWNLOAD_DIAGNOSTICS_LOGGER:
                # The exact diagnostics logger has a smaller, independently
                # validated field set. In particular it never serializes
                # internal_reason or other general-purpose string extras.
                if field not in {"duration_ms", "error_code"}:
                    continue
                value = _safe_download_diagnostic_value(field, value)
            if value is not None:
                payload[field] = value
        if record.name == _PAYMENT_AUDIT_LOGGER:
            for field in _PAYMENT_AUDIT_FIELDS:
                value = _safe_payment_audit_value(field, getattr(record, field, None))
                if value is not None:
                    payload[field] = value
        if record.name == _PAYMENT_CALLBACK_DIAGNOSTIC_LOGGER:
            for field in _PAYMENT_CALLBACK_DIAGNOSTIC_FIELDS:
                value = _safe_callback_diagnostic_value(
                    field, getattr(record, field, None)
                )
                if value is not None:
                    payload[field] = value
        if record.name == _DOWNLOAD_DIAGNOSTICS_LOGGER:
            for field in _DOWNLOAD_DIAGNOSTIC_FIELDS:
                value = _safe_download_diagnostic_value(
                    field, getattr(record, field, None)
                )
                if value is not None:
                    payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger for structured JSON output to stdout."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Quiet noisy third-party loggers in production-shaped output.
    # httpx/httpcore log full request URLs (including query) at INFO — never
    # allow that in FetchNow process output.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
