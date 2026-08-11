"""Media URL validation, diagnostic probe, and wrapper resolution API."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fetchnow.core.config import Settings
from fetchnow.core.errors import error_envelope
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.api_map import map_resolution_error
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.errors import ResolutionError
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import SystemDnsResolver
from fetchnow.url.errors import HTTP_STATUS_BY_CODE, URLValidationError
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

router = APIRouter(prefix="/media", tags=["media"])
logger = logging.getLogger("fetchnow.api.media")


class MediaURLRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)


def _get_dns_resolver(request: Request) -> SystemDnsResolver:
    existing = getattr(request.app.state, "dns_resolver", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    # Fail closed: do not invent a divergent DNS resolver when lifespan did
    # not wire one (tests must inject FakeDnsResolver via app.state).
    raise RuntimeError("dns_resolver is not configured on application state")


def _get_validator(request: Request) -> URLValidator:
    existing = getattr(request.app.state, "url_validator", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    settings: Settings = request.app.state.settings
    dns = _get_dns_resolver(request)
    provider_registry = getattr(request.app.state, "provider_registry", None)
    if provider_registry is None:
        provider_registry = ProviderRegistry.from_settings(settings)
        request.app.state.provider_registry = provider_registry
    validator = URLValidator(
        settings,
        registry=provider_registry,
        resolver=dns,
    )
    request.app.state.url_validator = validator
    return validator


def _get_http_client(request: Request) -> SafeHTTPClient:
    existing = getattr(request.app.state, "safe_http_client", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    settings: Settings = request.app.state.settings
    validator = _get_validator(request)
    dns = _get_dns_resolver(request)
    client = SafeHTTPClient(settings, validator=validator, resolver=dns)
    request.app.state.safe_http_client = client
    return client


def _get_resolution_service(request: Request) -> ResolutionService:
    existing = getattr(request.app.state, "resolution_service", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    # Fallback must reuse the same shared DNS/client/validator so security
    # configuration cannot diverge from lifespan wiring.
    settings: Settings = request.app.state.settings
    dns = _get_dns_resolver(request)
    validator = _get_validator(request)
    http_client = _get_http_client(request)
    provider_registry = getattr(request.app.state, "provider_registry", None)
    if provider_registry is None:
        provider_registry = ProviderRegistry.from_settings(settings)
        request.app.state.provider_registry = provider_registry
    wrapper_registry = getattr(request.app.state, "wrapper_registry", None)
    if wrapper_registry is None:
        wrapper_registry = build_wrapper_registry(settings, provider_registry)
        request.app.state.wrapper_registry = wrapper_registry
    service = ResolutionService(
        settings,
        provider_registry=provider_registry,
        wrapper_registry=wrapper_registry,
        validator=validator,
        http_client=http_client,
        dns_resolver=dns,
    )
    request.app.state.resolution_service = service
    return service


@router.post("/validate")
async def validate_media_url(
    payload: MediaURLRequest,
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


@router.post("/probe")
async def probe_media_url(
    payload: MediaURLRequest,
    request: Request,
) -> JSONResponse:
    """Diagnostic safe outbound probe (HEAD/limited GET). Not media extraction."""
    request_id = getattr(request.state, "request_id", None)
    started = time.perf_counter()
    client = _get_http_client(request)
    try:
        result = await client.probe(payload.url, method="HEAD")
    except URLValidationError as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "media_probe",
            extra={
                "request_id": request_id,
                "outcome": "error",
                "error_code": exc.code,
                "duration_ms": duration_ms,
                "route": "/api/v1/media/probe",
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
    body = {
        "provider": {
            "id": result.provider_id,
            "displayName": result.provider_display_name,
        },
        "finalUrl": {
            "scheme": result.final_scheme,
            "hostname": result.final_hostname,
            "path": result.final_path,
        },
        "http": {
            "status": result.status_code,
            "contentType": result.content_type,
            "contentLength": result.content_length,
            "redirectCount": result.redirect_count,
            "methodUsed": result.method_used,
        },
    }
    logger.info(
        "media_probe",
        extra={
            "request_id": request_id,
            "outcome": "allow",
            "provider_id": result.provider_id,
            "hostname": result.final_hostname,
            "duration_ms": duration_ms,
            "redirect_count": result.redirect_count,
            "body_bytes_read": result.body_bytes_read,
            "http_status": result.status_code,
            "http_method": result.method_used,
            "route": "/api/v1/media/probe",
        },
    )
    return JSONResponse(status_code=200, content=body)


@router.post("/resolve")
async def resolve_media_url(
    payload: MediaURLRequest,
    request: Request,
) -> JSONResponse:
    """Resolve a direct provider URL or a registered wrapper to a provider URL."""
    request_id = getattr(request.state, "request_id", None)
    started = time.perf_counter()
    service = _get_resolution_service(request)
    try:
        result = await service.resolve(payload.url)
    except ResolutionError as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        code, status, message = map_resolution_error(exc)
        logger.info(
            "media_resolve",
            extra={
                "request_id": request_id,
                "outcome": "error",
                "error_code": code,
                "duration_ms": duration_ms,
                "route": "/api/v1/media/resolve",
            },
        )
        return JSONResponse(
            status_code=status,
            content=error_envelope(
                code=code,
                message=message,
                request_id=request_id,
            ),
        )
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "media_resolve",
            extra={
                "request_id": request_id,
                "outcome": "error",
                "error_code": "INTERNAL_ERROR",
                "duration_ms": duration_ms,
                "route": "/api/v1/media/resolve",
            },
        )
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected resolution error occurred.",
                request_id=request_id,
            ),
        )

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    chain = [
        {
            "resolverId": hop.resolver_id,
            "wrapperType": hop.wrapper_type,
            "sourceCanonical": hop.source_canonical,
            "targetCanonical": hop.target_canonical,
            "strategy": hop.strategy,
        }
        for hop in result.resolution_chain
    ]
    body = {
        "provider": {
            "id": result.provider_id,
            "displayName": result.provider_display_name,
        },
        "url": {
            "scheme": result.provider_url.scheme,
            "hostname": result.provider_url.hostname,
            "port": result.provider_url.port,
            "path": result.provider_url.path,
            "canonical": result.canonical_provider_url,
        },
        "provenance": result.provenance.value,
        "wrapperType": result.wrapper_type,
        "resolutionChain": chain,
    }
    logger.info(
        "media_resolve",
        extra={
            "request_id": request_id,
            "outcome": "allow",
            "provider_id": result.provider_id,
            "hostname": result.provider_url.hostname,
            "provenance": result.provenance.value,
            "wrapper_type": result.wrapper_type,
            "chain_len": len(chain),
            "duration_ms": duration_ms,
            "route": "/api/v1/media/resolve",
        },
    )
    return JSONResponse(status_code=200, content=body)
