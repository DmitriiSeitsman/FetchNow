"""Public anonymous Free-download quota bootstrap and status API."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fetchnow.core.errors import error_envelope
from fetchnow.quota.errors import AnonymousIdentityRequiredError
from fetchnow.quota.service import QuotaService

router = APIRouter(prefix="/media", tags=["media-quota"])
logger = logging.getLogger("fetchnow.api.quota")
_NO_STORE = {"Cache-Control": "no-store"}


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not configured")
    return factory  # type: ignore[no-any-return]


def _quota_service(request: Request) -> QuotaService:
    service = getattr(request.app.state, "quota_service", None)
    if service is None:
        service = QuotaService(request.app.state.settings)
        request.app.state.quota_service = service
    return service


@router.get("/quota", response_model=None)
async def get_free_quota(request: Request) -> JSONResponse:
    """Bootstrap a stable identity and return effective rolling Free usage."""
    request_id = getattr(request.state, "request_id", None)
    settings = request.app.state.settings
    if not settings.free_download_quota_enabled:
        return JSONResponse(
            status_code=503,
            headers=_NO_STORE,
            content=error_envelope(
                code="FREE_QUOTA_DISABLED",
                message="Free download quota is not active.",
                request_id=request_id,
            ),
        )
    service = _quota_service(request)
    try:
        async with _session_factory(request)() as session:
            identity = await service.bootstrap_identity(
                cookie_header=request.headers.get("cookie"), session=session
            )
            status = await service.status(identity_id=identity.id, session=session)
            await session.commit()
    except AnonymousIdentityRequiredError:
        return JSONResponse(
            status_code=428,
            headers=_NO_STORE,
            content=error_envelope(
                code="FREE_QUOTA_IDENTITY_REQUIRED",
                message="A valid anonymous client identity is required.",
                request_id=request_id,
            ),
        )
    except Exception:
        logger.exception("free_quota_status_failed")
        return JSONResponse(
            status_code=500,
            headers=_NO_STORE,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )

    headers = dict(_NO_STORE)
    cookie = service.set_cookie_header(identity)
    if cookie is not None:
        headers["Set-Cookie"] = cookie
    return JSONResponse(
        status_code=200,
        headers=headers,
        content=service.to_public_dict(status),
    )
