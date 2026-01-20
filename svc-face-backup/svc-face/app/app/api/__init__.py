# services/svc-face/app/app/api/__init__.py
from fastapi import APIRouter
from app.api.health import router as health_router
from app.api.routes.face_jobs import router as face_jobs_router
from app.api.routes.creator_platform_endpoints import router as creator_platform_router

def build_router() -> APIRouter:
    """Build main API router"""
    router = APIRouter()
    
    router.include_router(health_router, prefix="/api", tags=["health"])
    router.include_router(face_jobs_router, prefix="/api/face", tags=["face"])
    router.include_router(creator_platform_router, tags=["creator_platform"])

    return router