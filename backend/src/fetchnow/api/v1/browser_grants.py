"""Public browser delivery grant issuance API (PR14)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fetchnow.core.errors import error_envelope
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.grant_service import BrowserGrantService
from fetchnow.jobs.credentials import parse_access_token
from fetchnow.jobs.errors import JobError, JobErrorCode

router = APIRouter(prefix="/media", tags=["media-browser-grants"])
logger = logging.getLogger("fetchnow.api.browser_grants")

_NO_STORE = {"Cache-Control": "no-store"}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not configured")
    return factory  # type: ignore[no-any-return]


def _grant_service(request: Request) -> BrowserGrantService:
    existing = getattr(request.app.state, "browser_grant_service", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    service = BrowserGrantService(request.app.state.settings)
    request.app.state.browser_grant_service = service
    return service


def _bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _error_response(exc: DownloadError, request_id: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        headers=_NO_STORE,
        content=error_envelope(
            code=exc.code.value,
            message=exc.message,
            request_id=request_id,
        ),
    )


def _not_found(request_id: str | None) -> JSONResponse:
    return _error_response(
        DownloadError(DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND),
        request_id,
    )


def _parse_bearer_or_not_found(
    authorization: str | None, request_id: str | None
) -> str | JSONResponse:
    token = _bearer_token(authorization)
    if token is None:
        return _not_found(request_id)
    try:
        parse_access_token(token)
    except JobError as exc:
        if exc.code is JobErrorCode.INVALID_ACCESS_TOKEN:
            return _not_found(request_id)
        return _error_response(
            DownloadError(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="TOKEN_LAYER",
            ),
            request_id,
        )
    return token


@router.post(
    "/download-jobs/{download_job_id}/browser-grants",
    status_code=201,
    response_model=None,
)
async def create_browser_grant(
    download_job_id: uuid.UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Issue a short-lived HttpOnly cookie grant for native browser download."""
    request_id = _request_id(request)
    parsed = _parse_bearer_or_not_found(authorization, request_id)
    if not isinstance(parsed, str):
        logger.info("browser_delivery_grant_rejected outcome=unauthorized")
        return parsed
    token = parsed

    service = _grant_service(request)
    session_factory = _session_factory(request)
    try:
        async with session_factory() as session:
            issued = await service.issue(
                download_job_id=download_job_id,
                access_token=token,
                session=session,
            )
            await session.commit()
    except DownloadError as exc:
        logger.info(
            "browser_delivery_grant_rejected outcome=%s",
            exc.code.value,
        )
        return _error_response(exc, request_id)
    except Exception:
        # Never log exception text/repr/SQL params (may contain token_hash).
        logger.info("browser_delivery_grant_failed outcome=internal")
        return JSONResponse(
            status_code=500,
            headers=_NO_STORE,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )

    body = service.to_public_dict(issued)
    headers = {
        **_NO_STORE,
        "Set-Cookie": service.set_cookie_header(issued),
    }
    return JSONResponse(status_code=201, content=body, headers=headers)
