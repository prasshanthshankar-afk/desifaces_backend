"""Hidden V3-only probe for certifying Fusion compatibility translation.

The router is conditionally mounted only when the V3 canonical-adapter shadow
flag is enabled. It is excluded from OpenAPI and performs no generation,
pricing reservation, persistence mutation, recovery action, or provider call.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.api.deps import check_fusion_enabled, get_current_user_id
from app.db import get_pool
from app.services.v3_fusion_adapter_shadow import (
    AccountContextNotFound,
    build_fusion_v3_shadow_mapping,
)

router = APIRouter()


@router.post(
    "/internal/v3/fusion-adapter/map",
    include_in_schema=False,
    dependencies=[Depends(check_fusion_enabled)],
)
async def map_fusion_request_to_v3(
    payload: Dict[str, Any] = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    pool = await get_pool()
    try:
        result = await build_fusion_v3_shadow_mapping(
            pool,
            user_id=user_id,
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
            detail="v3_fusion_adapter_probe_disabled",
        )

    return result.model_dump(mode="json")
