"""Hidden V3-only probe for certifying Pricing compatibility translation.

The route is mounted only when the V3 canonical-adapter shadow flag is enabled,
is excluded from OpenAPI, and performs no quote execution, reservation, payment,
ledger mutation, bootstrap, or provider call.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, status

from app.api.deps import AuthContext, AuthDep, PoolDep
from app.services.v3_pricing_adapter_shadow import (
    AccountContextNotFound,
    build_pricing_v3_shadow_mapping,
)

router = APIRouter()


@router.post(
    "/internal/v3/pricing-adapter/map-preview",
    include_in_schema=False,
)
async def map_pricing_preview_to_v3(
    payload: Dict[str, Any] = Body(...),
    auth: AuthContext = AuthDep,
    pool=PoolDep,
) -> Dict[str, Any]:
    try:
        result = await build_pricing_v3_shadow_mapping(
            pool,
            user_id=auth.user_id,
            payload=payload,
        )
    except AccountContextNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "v3_account_context_missing",
                "message": str(exc),
            },
        ) from exc

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="v3_pricing_adapter_probe_disabled",
        )

    return result.model_dump(mode="json")
