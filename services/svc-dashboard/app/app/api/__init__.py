from __future__ import annotations

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.routes.dashboard import router as dashboard_router


def build_router() -> APIRouter:
    router = APIRouter()
    router.include_router(health_router)
    router.include_router(dashboard_router)
    return router
