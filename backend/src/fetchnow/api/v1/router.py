"""API v1 router aggregation."""

from fastapi import APIRouter

from fetchnow.api.v1 import browser_grants, health, media, media_downloads, media_jobs

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(media.router)
api_router.include_router(media_jobs.router)
api_router.include_router(media_downloads.router)
api_router.include_router(browser_grants.router)
