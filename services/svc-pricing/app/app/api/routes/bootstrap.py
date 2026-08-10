from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.api.deps import PoolDep
from app.api.routes.reservations import (
    _bootstrap_free_user_pricing_tx,
    _require_internal_pricing_caller,
)

router = APIRouter(tags=["pricing-bootstrap"])


class FreeUserBootstrapRequest(BaseModel):
    user_id: str
    email: str | None = None
    source: str = "svc_core_register"
    credits: int | None = None


class FreeUserBootstrapResponse(BaseModel):
    # Keep both ok/status for backward compatibility with older callers while
    # making the response match bootstrap_free_user_pricing(...).
    ok: bool = True
    status: str = "ok"
    user_id: str
    bootstrapped: bool
    tier_code: str | None = None
    plan_code: str | None = None
    included_credits_total: int | None = None
    included_credits_remaining: int | None = None
    granted_balance_credits: int | None = None
    target_credits: int | None = None
    active_lot_count: int | None = None
    included_granted: int | None = None
    included_remaining: int | None = None
    included_reserved: int | None = None
    core_tier_action: str | None = None
    billing_entitlement_action: str | None = None
    account_action: str | None = None
    lot_action: str | None = None
    reason: str | None = None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _response_from_result(*, user_id: str, result: Dict[str, Any]) -> FreeUserBootstrapResponse:
    return FreeUserBootstrapResponse(
        ok=True,
        status="ok" if bool(result.get("bootstrapped")) else "skipped",
        user_id=str(result.get("user_id") or user_id),
        bootstrapped=bool(result.get("bootstrapped")),
        tier_code=(str(result.get("tier_code")) if result.get("tier_code") else None),
        plan_code=(str(result.get("plan_code")) if result.get("plan_code") else None),
        included_credits_total=_optional_int(result.get("included_credits_total")),
        included_credits_remaining=_optional_int(result.get("included_credits_remaining")),
        granted_balance_credits=_optional_int(
            result.get("granted_balance_credits")
            if result.get("granted_balance_credits") is not None
            else result.get("included_remaining")
        ),
        target_credits=_optional_int(result.get("target_credits")),
        active_lot_count=_optional_int(result.get("active_lot_count")),
        included_granted=_optional_int(result.get("included_granted")),
        included_remaining=_optional_int(result.get("included_remaining")),
        included_reserved=_optional_int(result.get("included_reserved")),
        core_tier_action=(str(result.get("core_tier_action")) if result.get("core_tier_action") else None),
        billing_entitlement_action=(
            str(result.get("billing_entitlement_action"))
            if result.get("billing_entitlement_action")
            else None
        ),
        account_action=(str(result.get("account_action")) if result.get("account_action") else None),
        lot_action=(str(result.get("lot_action")) if result.get("lot_action") else None),
        reason=(str(result.get("reason")) if result.get("reason") else None),
    )


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
        result = await _bootstrap_free_user_pricing_tx(
            conn,
            user_id=user_uuid,
            email=(req.email or None),
            source=req.source or "svc_core_register",
            credits=req.credits,
        )

    return _response_from_result(user_id=req.user_id, result=result)
