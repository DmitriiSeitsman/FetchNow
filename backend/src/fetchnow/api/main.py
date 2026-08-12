"""FastAPI application factory and process entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from fetchnow.api.v1.router import api_router
from fetchnow.core.config import Settings, get_settings
from fetchnow.core.errors import register_exception_handlers
from fetchnow.core.logging import configure_logging
from fetchnow.core.middleware import RequestIdMiddleware
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.downloads.service import DownloadJobService
from fetchnow.jobs.service import MediaJobService
from fetchnow.media_inspection.defaults import build_default_inspection_registry
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import SystemDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        resolver = SystemDnsResolver(
            timeout_seconds=settings.dns_resolution_timeout_seconds
        )
        provider_registry = ProviderRegistry.from_settings(settings)
        validator = URLValidator(
            settings, registry=provider_registry, resolver=resolver
        )
        http_client = SafeHTTPClient(
            settings,
            validator=validator,
            resolver=resolver,
        )
        wrapper_registry = build_wrapper_registry(settings, provider_registry)
        resolution_service = ResolutionService(
            settings,
            provider_registry=provider_registry,
            wrapper_registry=wrapper_registry,
            validator=validator,
            http_client=http_client,
            dns_resolver=resolver,
        )
        # Registry for ownership checks only — API never runs extractors/yt-dlp.
        inspection_registry = build_default_inspection_registry(
            settings, provider_registry
        )
        media_job_service = MediaJobService(settings)
        download_job_service = DownloadJobService(settings)
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.settings = settings
        app.state.dns_resolver = resolver
        app.state.provider_registry = provider_registry
        app.state.url_validator = validator
        app.state.safe_http_client = http_client
        app.state.wrapper_registry = wrapper_registry
        app.state.resolution_service = resolution_service
        app.state.inspection_registry = inspection_registry
        app.state.media_job_service = media_job_service
        app.state.download_job_service = download_job_service
        try:
            yield
        finally:
            await http_client.aclose()
            await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.app_env == "production" else "/docs",
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


def run() -> None:
    """CLI entrypoint for the API process."""
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "fetchnow.api.main:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


app = create_app()
