"""Public media-download-job create/status API (PR6)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fetchnow.core.errors import error_envelope
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.service import DownloadJobService

router = APIRouter(prefix="/media", tags=["media-downloads"])
logger = logging.getLogger("fetchnow.api.media_downloads")

_NO_STORE = {"Cache-Control": "no-store"}


class DownloadCreateRequest(BaseModel):
    format_option_id: str = Field(min_length=1, max_length=64, alias="formatOptionId")

    model_config = {"populate_by_name": True}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not configured on application state")
    return factory  # type: ignore[no-any-return]


def _get_download_service(request: Request) -> DownloadJobService:
    existing = getattr(request.app.state, "download_job_service", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    service = DownloadJobService(request.app.state.settings)
    request.app.state.download_job_service = service
    return service


def _bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _download_error_response(
    exc: DownloadError, request_id: str | None
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        headers=_NO_STORE,
        content=error_envelope(
            code=exc.code.value,
            message=exc.message,
            request_id=request_id,
        ),
    )


@router.post("/jobs/{media_job_id}/downloads", status_code=202, response_model=None)
async def create_download_job(
    media_job_id: uuid.UUID,
    payload: DownloadCreateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Enqueue a durable download job for an inspected parent MediaJob."""
    request_id = _request_id(request)
    token = _bearer_token(authorization)
    if token is None:
        return _download_error_response(
            DownloadError(DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND),
            request_id,
        )

    service = _get_download_service(request)
    session_factory = _get_session_factory(request)
    try:
        async with session_factory() as session:
            view = await service.create(
                media_job_id=media_job_id,
                format_option_id=payload.format_option_id,
                access_token=token,
                session=session,
            )
            await session.commit()
    except DownloadError as exc:
        return _download_error_response(exc, request_id)
    except Exception:
        logger.exception("media_download_create_failed")
        return JSONResponse(
            status_code=500,
            headers=_NO_STORE,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )

    body = service.to_public_dict(view)
    return JSONResponse(status_code=202, content=body, headers=_NO_STORE)


@router.get("/download-jobs/{download_job_id}", response_model=None)
async def get_download_job(
    download_job_id: uuid.UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Read download-job status authorized via parent MediaJob Bearer token."""
    request_id = _request_id(request)
    token = _bearer_token(authorization)
    if token is None:
        return _download_error_response(
            DownloadError(DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND),
            request_id,
        )

    service = _get_download_service(request)
    session_factory = _get_session_factory(request)
    try:
        async with session_factory() as session:
            view = await service.get(
                download_job_id=download_job_id,
                access_token=token,
                session=session,
            )
    except DownloadError as exc:
        return _download_error_response(exc, request_id)
    except Exception:
        logger.exception("media_download_get_failed")
        return JSONResponse(
            status_code=500,
            headers=_NO_STORE,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )

    body = service.to_public_dict(view)
    return JSONResponse(status_code=200, content=body, headers=_NO_STORE)


@router.post("/download-jobs/{download_job_id}/cancel", response_model=None)
async def cancel_download_job(
    download_job_id: uuid.UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Idempotent user cancel. UUID alone never authorizes."""
    request_id = _request_id(request)
    token = _bearer_token(authorization)
    if token is None:
        return _download_error_response(
            DownloadError(DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND),
            request_id,
        )

    service = _get_download_service(request)
    session_factory = _get_session_factory(request)
    try:
        async with session_factory() as session:
            view = await service.cancel(
                download_job_id=download_job_id,
                access_token=token,
                session=session,
            )
            await session.commit()
    except DownloadError as exc:
        return _download_error_response(exc, request_id)
    except Exception:
        logger.exception("media_download_cancel_failed")
        return JSONResponse(
            status_code=500,
            headers=_NO_STORE,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )

    body = service.to_public_dict(view)
    return JSONResponse(status_code=200, content=body, headers=_NO_STORE)
