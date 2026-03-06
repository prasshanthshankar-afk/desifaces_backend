# services/svc-pricing/app/app/api/routes/health.py
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
async def health() -> dict:
    return {"ok": True}