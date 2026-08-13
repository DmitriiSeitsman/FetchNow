"""Dedicated delivery ASGI application and process entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import APIRouter, FastAPI

from fetchnow.api.v1.health import router as health_router
from fetchnow.core.config import Settings, get_settings
from fetchnow.core.errors import register_exception_handlers
from fetchnow.core.logging import configure_logging
from fetchnow.core.middleware import RequestIdMiddleware
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.delivery.routes import router as delivery_router
from fetchnow.delivery.service import DeliveryService
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error


def create_delivery_app(settings: Settings | None = None) -> FastAPI:
    """Build the delivery-only FastAPI application.

    Delivery-safe surface: health + authenticated content streaming. No
    inspection/download/worker tooling is constructed.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    if settings.media_delivery_enabled and not settings.media_delivery_root.strip():
        # Fail closed at process start when the feature is on without a root.
        raise_download_error(
            DownloadErrorCode.DELIVERY_DISABLED,
            internal_reason="DELIVERY_ROOT_REQUIRED",
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        delivery_service = DeliveryService(settings)
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.settings = settings
        app.state.delivery_service = delivery_service
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title=f"{settings.app_name} Delivery",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.app_env == "production" else "/docs",
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    api = APIRouter(prefix="/api/v1")
    api.include_router(health_router)
    api.include_router(delivery_router)
    app.include_router(api)
    return app


def run() -> None:
    """CLI entrypoint for the delivery process."""
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "fetchnow.delivery.main:create_delivery_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


app = create_delivery_app()
