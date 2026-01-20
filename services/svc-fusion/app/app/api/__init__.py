from __future__ import annotations

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.routes.fusion_jobs import router as fusion_jobs_router


def build_router() -> APIRouter:
    r = APIRouter()
    r.include_router(health_router)
    r.include_router(fusion_jobs_router)
    return r