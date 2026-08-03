"""API v1 router aggregation."""

from fastapi import APIRouter

from fetchnow.api.v1 import health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
