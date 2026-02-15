from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import UUID, uuid4

try:
    import httpx  # optional; only used when svc-pricing exists
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

from app.domain.models import (
    CommerceQuoteIn,
    CommerceQuoteOut,
    QuoteBreakdownItem,
    QuoteLineItem,
)


@dataclass
class PricingConfig:
    pricing_service_url: str | None
    quote_ttl_seconds: int
    credit_usd_rate: float
    credit_inr_rate: float


def _cfg() -> PricingConfig:
    return PricingConfig(
        pricing_service_url=os.getenv("PRICING_SERVICE_URL") or None,
        quote_ttl_seconds=int(os.getenv("COMMERCE_QUOTE_TTL_SECONDS", "900")),
        credit_usd_rate=float(os.getenv("COMMERCE_CREDIT_USD_RATE", "0.03")),
        credit_inr_rate=float(os.getenv("COMMERCE_CREDIT_INR_RATE", "2.5")),
    )


def _val(x: Any) -> Any:
    """Return enum.value if present, else the object itself."""
    return getattr(x, "value", x)


class PricingClient:
    """
    Placeholder that can later call svc-pricing.
    Today:
      - If PRICING_SERVICE_URL is set and httpx exists, it attempts HTTP.
      - Otherwise it uses a deterministic local calculator.
    """

    async def quote(self, *, user_id: UUID, req: CommerceQuoteIn) -> CommerceQuoteOut:
        cfg = _cfg()

        # Future: call svc-pricing
        if cfg.pricing_service_url and httpx:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(
                        f"{cfg.pricing_service_url.rstrip('/')}/api/pricing/quote",
                        json=req.model_dump(mode="json"),
                        headers={"X-User-Id": str(user_id)},
                    )
                    r.raise_for_status()
                    return CommerceQuoteOut(**r.json())
            except Exception:
                # Fall back to local calculator (never block UX)
                pass

        return self._local_quote(user_id=user_id, req=req, ttl_seconds=cfg.quote_ttl_seconds)

    def _local_quote(self, *, user_id: UUID, req: CommerceQuoteIn, ttl_seconds: int) -> CommerceQuoteOut:
        quote_id = uuid4()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)

        # count references (looksets/products). If none provided, treat as 1.
        n_refs = max(len(req.look_set_ids), len(req.product_ids), 1)
        ref_ids = [str(x) for x in (req.look_set_ids or req.product_ids or [uuid4()])]

        # Base credit unit costs
        c_img = int(os.getenv("COMMERCE_CREDITS_PER_IMAGE", "30"))
        c_vid = int(os.getenv("COMMERCE_CREDITS_PER_VIDEO", "120"))

        # Resolution multiplier
        res = _val(req.resolution)
        res_mult_map = {"hd": 1.0, "4k": 1.5}
        res_mult = float(res_mult_map.get(str(res), 1.0))

        # Mode multiplier (safe defaults)
        mode = str(_val(req.mode))
        mode_mult_map = {
            "autopilot": 1.0,
            "co_create": 1.05,
            "bring_your_own": 0.9,
            "byo": 0.9,
            "upload": 0.9,
        }
        mode_mult = float(mode_mult_map.get(mode, 1.0))

        # Product-type multiplier (safe defaults)
        ptype = str(_val(req.product_type))
        ptype_mult_map = {
            "apparel": 1.0,
            "fmcg": 0.95,
            "electronics": 1.05,
        }
        ptype_mult = float(ptype_mult_map.get(ptype, 1.0))

        # People multiplier
        people_mult_map = {
            "solo_female": 1.0,
            "solo_male": 1.0,
            "couple": 1.8,
            "group3": 2.4,
        }
        people = req.people or ["solo_female"]
        people_mult = max([people_mult_map.get(p, 1.0) for p in people])

        # Views multiplier
        view_mult = 1.0
        if req.views.full_body and req.views.half_body:
            view_mult = 1.15
        elif req.views.full_body or req.views.half_body:
            view_mult = 1.0

        # Template pack multiplier
        template_mult = 1.0 if (req.template_pack or "default") == "default" else 1.1

        # CTA small add-on (optional)
        cta_mult = 1.0
        cta_type = (req.cta.type or "none").lower()
        if cta_type in ("whatsapp", "url"):
            cta_mult = 1.02

        # Channel export cost (small)
        export_per_channel = int(os.getenv("COMMERCE_CREDITS_PER_CHANNEL_EXPORT", "10"))
        export_credits = export_per_channel * len(req.channels)

        # Marketplace add-on pack cost
        mp_pack = int(os.getenv("COMMERCE_CREDITS_MARKETPLACE_PACK", "25"))
        marketplace_credits = mp_pack * len(req.marketplaces)

        mult = res_mult * mode_mult * ptype_mult * people_mult * view_mult * template_mult * cta_mult

        per_ref_img = int(req.outputs.num_images * c_img * mult)
        per_ref_vid = int(req.outputs.num_videos * c_vid * mult)
        per_ref_total = per_ref_img + per_ref_vid + export_credits + marketplace_credits

        breakdown: List[QuoteBreakdownItem] = []
        for rid in ref_ids[:n_refs]:
            items: List[QuoteLineItem] = [
                QuoteLineItem(type="image", qty=req.outputs.num_images, credits=per_ref_img),
                QuoteLineItem(type="video", qty=req.outputs.num_videos, credits=per_ref_vid),
            ]
            if export_credits:
                items.append(QuoteLineItem(type="channel_exports", qty=len(req.channels), credits=export_credits))
            if marketplace_credits:
                items.append(QuoteLineItem(type="marketplace_pack", qty=len(req.marketplaces), credits=marketplace_credits))

            breakdown.append(QuoteBreakdownItem(ref_id=rid, credits=per_ref_total, items=items))

        total_credits = per_ref_total * n_refs
        cfg = _cfg()
        usd = round(total_credits * cfg.credit_usd_rate, 2)
        inr = round(total_credits * cfg.credit_inr_rate, 2)

        return CommerceQuoteOut(
            quote_id=quote_id,
            total_credits=total_credits,
            totals={"usd": usd, "inr": inr},
            breakdown=breakdown,
            expires_at=expires_at,
            expires_in_seconds=ttl_seconds,
            assumptions={
                "note": "Local pricing stub. Set PRICING_SERVICE_URL later to call svc-pricing.",
                "credits_per_image": c_img,
                "credits_per_video": c_vid,
                "mult": mult,
                "resolution": str(res),
                "mode": mode,
                "product_type": ptype,
                "people_multiplier": people_mult,
                "view_multiplier": view_mult,
                "template_multiplier": template_mult,
                "cta_multiplier": cta_mult,
            },
        )