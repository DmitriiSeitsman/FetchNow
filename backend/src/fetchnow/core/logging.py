"""Structured JSON logging for FetchNow processes."""

from __future__ import annotations

import json
import logging
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
