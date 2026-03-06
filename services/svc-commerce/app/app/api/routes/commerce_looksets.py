from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import require_user

router = APIRouter(prefix="/api/commerce/looksets", tags=["commerce"])


@router.get("/ping", operation_id="commerce_looksets_ping")
async def ping(user_id: UUID = Depends(require_user)):
    return {"ok": True}