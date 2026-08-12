"""Public media-inspection job create/status API (PR5)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.core.errors import error_envelope
from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.service import MediaJobService
from fetchnow.media_inspection.defaults import build_default_inspection_registry
from fetchnow.media_inspection.errors import InspectionError
from fetchnow.resolution.api_map import map_resolution_error
from fetchnow.resolution.errors import ResolutionError, ResolutionErrorKind
from fetchnow.url.errors import HTTP_STATUS_BY_CODE, URLValidationError
from fetchnow.url.providers import ProviderRegistry

router = APIRouter(prefix="/media", tags=["media-jobs"])
logger = logging.getLogger("fetchnow.api.media_jobs")


class MediaJobCreateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not configured on application state")
    return factory  # type: ignore[no-any-return]


def _get_resolution_service(request: Request) -> Any:
    from fetchnow.api.v1.media import _get_resolution_service as _resolve

    return _resolve(request)


def _get_job_service(request: Request) -> MediaJobService:
    existing = getattr(request.app.state, "media_job_service", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    service = MediaJobService(_get_settings(request))
    request.app.state.media_job_service = service
    return service


def _get_inspection_registry(request: Request) -> Any:
    existing = getattr(request.app.state, "inspection_registry", None)
    if existing is not None:
        return existing
    settings = _get_settings(request)
    providers = getattr(request.app.state, "provider_registry", None)
    if providers is None:
        providers = ProviderRegistry.from_settings(settings)
        request.app.state.provider_registry = providers
    # Ownership checks only — API never invokes yt-dlp / extractors.
    registry = build_default_inspection_registry(settings, providers)
    request.app.state.inspection_registry = registry
    return registry


def _bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _job_error_response(exc: JobError, request_id: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        headers={"Cache-Control": "no-store"},
        content=error_envelope(
            code=exc.code.value,
            message=exc.message,
            request_id=request_id,
        ),
    )


@router.post("/jobs", status_code=202, response_model=None)
async def create_media_job(
    payload: MediaJobCreateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse | dict[str, Any]:
    """Enqueue a durable media-inspection job after ResolutionService."""
    request_id = _request_id(request)
    service = _get_job_service(request)
    resolution_service = _get_resolution_service(request)
    session_factory = _get_session_factory(request)
    inspection_registry = _get_inspection_registry(request)
    providers = getattr(request.app.state, "provider_registry", None)
    token = _bearer_token(authorization)
    if token is None:
        return _job_error_response(
            JobError(JobErrorCode.INVALID_ACCESS_TOKEN), request_id
        )

    try:
        async with session_factory() as session:
            created = await service.create_job(
                raw_url=payload.url,
                access_token=token,
                resolution_service=resolution_service,
                session=session,
                inspection_registry=inspection_registry,
                provider_registry=providers,
            )
            await session.commit()
    except JobError as exc:
        return _job_error_response(exc, request_id)
    except URLValidationError as exc:
        status = HTTP_STATUS_BY_CODE.get(exc.code, 500)
        return JSONResponse(
            status_code=status,
            headers={"Cache-Control": "no-store"},
            content=error_envelope(
                code=exc.code,
                message=exc.message,
                request_id=request_id,
            ),
        )
    except ResolutionError as exc:
        code, status, message = map_resolution_error(exc)
        return JSONResponse(
            status_code=status,
            headers={"Cache-Control": "no-store"},
            content=error_envelope(
                code=code,
                message=message,
                request_id=request_id,
            ),
        )
    except InspectionError:
        # Identity/policy failures during enqueue map to unsupported destination.
        code, status, message = map_resolution_error(
            ResolutionError(ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED)
        )
        return JSONResponse(
            status_code=status,
            headers={"Cache-Control": "no-store"},
            content=error_envelope(
                code=code,
                message=message,
                request_id=request_id,
            ),
        )
    except Exception:
        logger.exception("media_job_create_failed")
        return JSONResponse(
            status_code=500,
            headers={"Cache-Control": "no-store"},
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )

    body = service.job_to_public_dict(created.job)
    body["statusPath"] = f"/api/v1/media/jobs/{created.job.id}"
    return JSONResponse(
        status_code=202,
        content=body,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/jobs/{job_id}", response_model=None)
async def get_media_job(
    job_id: uuid.UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse | dict[str, Any]:
    """Read job status with Bearer access token (hashed ownership)."""
    request_id = _request_id(request)
    token = _bearer_token(authorization)
    if token is None:
        # Indistinguishable from unknown job — never confirm UUID existence.
        return _job_error_response(
            JobError(JobErrorCode.JOB_NOT_FOUND),
            request_id,
        )

    service = _get_job_service(request)
    session_factory = _get_session_factory(request)
    try:
        async with session_factory() as session:
            view = await service.get_job(
                job_id=job_id,
                access_token=token,
                session=session,
            )
    except JobError as exc:
        return _job_error_response(exc, request_id)
    except Exception:
        logger.exception("media_job_get_failed")
        return JSONResponse(
            status_code=500,
            headers={"Cache-Control": "no-store"},
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )

    body = service.job_to_public_dict(view)
    body["statusPath"] = f"/api/v1/media/jobs/{view.id}"
    return JSONResponse(
        status_code=200,
        content=body,
        headers={"Cache-Control": "no-store"},
    )
