from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import require_user

router = APIRouter(prefix="/api/commerce/tryon", tags=["commerce"])


@router.get("/help", operation_id="commerce_tryon_help")
async def help_(user_id: UUID = Depends(require_user)):
    return {
        "quote": {"method": "POST", "path": "/api/commerce/quote"},
        "confirm": {"method": "POST", "path": "/api/commerce/confirm"},
        "status": {"method": "GET", "path": "/api/commerce/jobs/{studio_job_id}/status"},
        "note": "Primary endpoints live in commerce_quotes.py for now.",
    }