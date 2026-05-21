from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.api.deps import PoolDep
from app.services.entitlements.free_signup_bootstrap_service import (
    bootstrap_free_user_pricing,
)
from app.api.routes.reservations import _require_internal_pricing_caller

router = APIRouter(tags=["pricing-bootstrap"])


class FreeUserBootstrapRequest(BaseModel):
    user_id: str
    email: str | None = None
    source: str = "svc_core_register"


class FreeUserBootstrapResponse(BaseModel):
    ok: bool = True
    user_id: str
    tier_code: str
    plan_code: str | None = None
    included_credits_total: int
    included_credits_remaining: int
    granted_balance_credits: int


@router.post("/api/pricing/bootstrap/free-user", response_model=FreeUserBootstrapResponse)
async def bootstrap_free_user(
    req: FreeUserBootstrapRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> FreeUserBootstrapResponse:
    caller = _require_internal_pricing_caller(
        authorization,
        x_user_id,
        x_service_name,
    )

    if str(req.user_id) != str(caller.user_id):
        raise HTTPException(status_code=403, detail="user_mismatch")

    try:
        user_uuid = UUID(str(req.user_id))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_user_id")

    async with pool.acquire() as conn:
        result = await bootstrap_free_user_pricing(
            conn,
            user_id=user_uuid,
            email=(req.email or None),
            source=req.source or "svc_core_register",
        )

    return FreeUserBootstrapResponse(
        ok=True,
        user_id=result["user_id"],
        tier_code=result["tier_code"],
        plan_code=result["plan_code"],
        included_credits_total=int(result["included_credits_total"]),
        included_credits_remaining=int(result["included_credits_remaining"]),
        granted_balance_credits=int(result["granted_balance_credits"]),
    )