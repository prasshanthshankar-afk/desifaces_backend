from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Request

from app.api.deps import get_current_token, get_current_user_id
from app.api.routes.longform import LongformPricingPreviewResponse, preview_longform


router = APIRouter(tags=["longform-compat"])


@router.post(
    "/api/longform/jobs/pricing/preview",
    response_model=LongformPricingPreviewResponse,
    include_in_schema=False,
)
@router.post(
    "/api/fusion/jobs/pricing/preview",
    response_model=LongformPricingPreviewResponse,
    include_in_schema=False,
)
@router.post(
    "/jobs/pricing/preview",
    response_model=LongformPricingPreviewResponse,
    include_in_schema=False,
)
async def legacy_parent_pricing_preview(
    request: Request,
    raw_req: Dict[str, Any] = Body(...),
    user_id: str = Depends(get_current_user_id),
    request_token: str = Depends(get_current_token),
) -> LongformPricingPreviewResponse:
    """Compatibility aliases for legacy parent-pricing callers.

    Parent billing authority remains the canonical Fusion Extension longform
    pricing handler. These aliases only preserve older route contracts.
    """
    return await preview_longform(
        request=request,
        raw_req=raw_req,
        user_id=user_id,
        _request_token=request_token,
    )
