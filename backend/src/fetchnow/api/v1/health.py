"""Health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from fetchnow.core.errors import error_envelope
from fetchnow.db.session import check_database

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Process is alive; no dependency checks."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    """Process is ready to serve traffic; requires PostgreSQL connectivity."""
    engine: AsyncEngine = request.app.state.engine
    request_id = getattr(request.state, "request_id", None)
    healthy = await check_database(engine)
    if not healthy:
        return JSONResponse(
            status_code=503,
            content=error_envelope(
                code="not_ready",
                message="PostgreSQL is unavailable",
                request_id=request_id,
            ),
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
