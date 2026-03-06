# services/svc-marketing/app/app/services/orchestration/recipes/commerce_catalog_promo.py
from __future__ import annotations

from typing import Any, Dict

from app.domain.models import MarketingRunIn, UseCaseSpec
from app.services.orchestration.run_context import RunContext
from app.services.orchestration.utils.media_extract import extract_media


class CommerceCatalogPromoRecipe:
    def __init__(self, commerce_client):
        self.commerce = commerce_client

    async def run(self, *, ctx: RunContext, use_case: UseCaseSpec, inp: MarketingRunIn, run_seed: int, request_nonce: str) -> Dict[str, Any]:
        payload = (inp.inputs or {}).get("commerce_payload") or {
            "use_case": "catalog_promo",
            "season_event": getattr(use_case, "season_event", None),
            "offer": getattr(use_case, "offer", None),
            "seconds": getattr(use_case, "target_seconds", 15) or 15,
            "inputs": inp.inputs,
        }

        resp = await self.commerce.create(ctx.to_downstream(), payload)
        media = extract_media(resp)
        return {"commerce_raw": resp, "commerce_job": media.get("job_id"), "reel_url": media.get("video_url")}