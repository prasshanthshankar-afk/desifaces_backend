from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/commerce/templates", tags=["commerce"])


@router.get("/ping")
async def ping():
    return {"ok": True}