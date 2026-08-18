"""Hidden V3-only probe for certifying Audio compatibility translation.

The router is conditionally mounted only in the V3 runtime. It is excluded from
OpenAPI and performs no TTS generation, translation, pricing reservation,
persistence mutation, storage write, or provider call.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.api.deps import get_current_user_id
from app.db import get_pool
from app.services.v3_audio_adapter_shadow import (
    AccountContextNotFound,
    build_audio_v3_shadow_mapping,
)

router = APIRouter()


@router.post(
    "/internal/v3/audio-adapter/map",
    include_in_schema=False,
)
async def map_audio_request_to_v3(
    payload: Dict[str, Any] = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Return the canonical mapping for an authenticated Audio request."""

    pool = await get_pool()
    try:
        result = await build_audio_v3_shadow_mapping(
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
            detail="v3_audio_adapter_probe_disabled",
        )

    return result.model_dump(mode="json")
