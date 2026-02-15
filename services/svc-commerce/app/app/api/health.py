from __future__ import annotations

import os
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "svc-commerce",
        "version": os.getenv("SERVICE_VERSION", "1.0.0"),
    }