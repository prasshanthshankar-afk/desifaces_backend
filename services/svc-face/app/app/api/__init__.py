from __future__ import annotations

from fastapi import APIRouter

from .health import router as health_router
from .routes.face_jobs import router as face_jobs_router


def build_router() -> APIRouter:
    router = APIRouter()

    router.include_router(health_router, prefix="/api/health", tags=["health"])
    router.include_router(face_jobs_router, prefix="/api/face", tags=["face"])

    return router