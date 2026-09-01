"""Server-authoritative Premium entitlement status API."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fetchnow.core.errors import error_envelope
from fetchnow.premium.service import PremiumEntitlementService
from fetchnow.quota.errors import AnonymousIdentityRequiredError
from fetchnow.quota.service import QuotaService

router = APIRouter(prefix="/premium", tags=["premium"])
logger = logging.getLogger("fetchnow.api.premium")
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


def _premium_service(request: Request) -> PremiumEntitlementService:
    service = getattr(request.app.state, "premium_service", None)
    if service is None:
        service = PremiumEntitlementService()
        request.app.state.premium_service = service
    return service


@router.get("/status", response_model=None)
async def get_premium_status(request: Request) -> JSONResponse:
    """Return the caller's active Premium entitlement, if any."""
    request_id = getattr(request.state, "request_id", None)
    try:
        async with _session_factory(request)() as session:
            identity = await _quota_service(request).require_identity(
                cookie_header=request.headers.get("cookie"), session=session
            )
            premium = _premium_service(request)
            status = await premium.status_for_client(
                anonymous_client_id=identity.id, session=session
            )
            from fetchnow.premium.repository import PremiumEntitlementRepository

            now = await PremiumEntitlementRepository(session).database_now()
            body = PremiumEntitlementService.public_dict(status, now=now)
            await session.commit()
    except AnonymousIdentityRequiredError:
        return JSONResponse(
            status_code=428,
            headers=_NO_STORE,
            content=error_envelope(
                code="PREMIUM_IDENTITY_REQUIRED",
                message="A valid anonymous client identity is required.",
                request_id=request_id,
            ),
        )
    except Exception:
        logger.exception("premium_status_failed")
        return JSONResponse(
            status_code=500,
            headers=_NO_STORE,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )
    return JSONResponse(status_code=200, headers=_NO_STORE, content=body)
