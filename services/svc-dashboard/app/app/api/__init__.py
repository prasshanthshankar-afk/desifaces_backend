from __future__ import annotations

from fastapi import APIRouter
from app.api.health import router as health_router


def build_router() -> APIRouter:
    r = APIRouter()
    r.include_router(health_router)
    return r