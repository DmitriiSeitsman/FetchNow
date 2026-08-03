"""Media URL validation API."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fetchnow.core.config import Settings
from fetchnow.core.errors import error_envelope
from fetchnow.url.dns import SystemDnsResolver
from fetchnow.url.errors import HTTP_STATUS_BY_CODE, URLValidationError
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

router = APIRouter(prefix="/media", tags=["media"])
logger = logging.getLogger("fetchnow.api.media")


class ValidateURLRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)


def _get_validator(request: Request) -> URLValidator:
    existing = getattr(request.app.state, "url_validator", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    settings: Settings = request.app.state.settings
    validator = URLValidator(
        settings,
        registry=ProviderRegistry.from_settings(settings),
        resolver=SystemDnsResolver(
            timeout_seconds=settings.dns_resolution_timeout_seconds
        ),
    )
    request.app.state.url_validator = validator
    return validator


@router.post("/validate")
async def validate_media_url(
    payload: ValidateURLRequest,
    request: Request,
) -> JSONResponse:
    """Validate and normalize a user-supplied media URL (no fetch)."""
    request_id = getattr(request.state, "request_id", None)
    started = time.perf_counter()
    validator = _get_validator(request)
    try:
        result = await validator.validate(payload.url)
    except URLValidationError as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "media_validate",
            extra={
                "request_id": request_id,
                "outcome": "error",
                "error_code": exc.code,
                "duration_ms": duration_ms,
                "route": "/api/v1/media/validate",
            },
        )
        status = HTTP_STATUS_BY_CODE.get(exc.code, 500)
        return JSONResponse(
            status_code=status,
            content=error_envelope(
                code=exc.code,
                message=exc.message,
                request_id=request_id,
            ),
        )

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    body: dict[str, Any] = {
        "provider": {
            "id": result.provider_id,
            "displayName": result.provider_display_name,
        },
        "url": {
            "scheme": result.url.scheme,
            "hostname": result.url.hostname,
            "port": result.url.port,
            "path": result.url.path,
            # Canonical omits query/fragment — query may carry secrets (PR0B redaction).
            "canonical": result.url.canonical,
        },
    }
    logger.info(
        "media_validate",
        extra={
            "request_id": request_id,
            "outcome": "allow",
            "provider_id": result.provider_id,
            "hostname": result.url.hostname,
            "duration_ms": duration_ms,
            "route": "/api/v1/media/validate",
        },
    )
    return JSONResponse(status_code=200, content=body)
