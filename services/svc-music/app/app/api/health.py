from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health():
    return {"ok": True}


@router.get("/ready")
async def ready():
    return {"ok": True}