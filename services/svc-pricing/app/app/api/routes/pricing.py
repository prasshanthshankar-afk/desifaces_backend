# services/svc-pricing/app/app/api/routes/pricing.py
from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import AuthContext, AuthDep, PoolDep
from app.services.engine.module_gate import evaluate_gate
from app.services.engine.pricing_engine import quote_variant

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


class QuoteIn(BaseModel):
    variant_code: str = Field(..., description="pricing_variants.code")
    params: Dict[str, Any] = Field(default_factory=dict)
    channel: str = Field(default="web", description="web|mobile|api")
    currency: Optional[str] = Field(default=None, description="USD|INR (optional)")
    country_code: Optional[str] = Field(default=None, description="ISO2 (optional; header also supported)")


class QuoteLineOut(BaseModel):
    sku_code: str
    name: str
    category: str
    provider_hint: Optional[str] = None
    unit: str
    qty: str
    unit_credits: int
    line_credits: int
    unit_money: Optional[str] = None
    line_money: str


class QuoteOut(BaseModel):
    allowed: bool
    billing_mode: str
    reason: str = ""

    variant_code: str
    module_code: str
    currency: str
    pricebook_id: UUID
    pricebook_name: str

    total_credits: int
    total_money: str
    alt_currency: Optional[str] = None
    alt_total_money: Optional[str] = None

    lines: list[QuoteLineOut]


@router.post("/quote", response_model=QuoteOut)
async def quote(inp: QuoteIn, auth: AuthContext = AuthDep, pool=PoolDep) -> QuoteOut:
    country = (inp.country_code or auth.country_code or "").upper()

    async with pool.acquire() as conn:
        # Determine module from variant category (read variant)
        # We do this by calling quote_variant (which fetches variant), but gate should happen first.
        # So we fetch the variant category cheaply first:
        v = await conn.fetchrow(
            "select category from pricing_variants where code = $1 and is_active=true",
            inp.variant_code,
        )
        if not v:
            raise HTTPException(status_code=404, detail="PRICING_UNKNOWN_OR_INACTIVE_VARIANT")

        category = str(v["category"])
        module_code = f"module.{category}"

        # Resolve tier for gating purposes (free fallback)
        ent = await conn.fetchrow("select tier_code from pricing_user_entitlements where user_id=$1", auth.user_id)
        if ent and ent.get("tier_code"):
            tier_code = str(ent["tier_code"])
        else:
            u = await conn.fetchrow("select tier from core.users where id=$1", auth.user_id)
            tier_code = str(u["tier"]) if u and u.get("tier") else "free"

        gate = await evaluate_gate(
            conn,
            module_code=module_code,
            channel=inp.channel,
            country_code=country,
            tier_code=tier_code,
        )
        if not gate.allowed:
            return QuoteOut(
                allowed=False,
                billing_mode=gate.billing_mode,
                reason=gate.reason or "MODULE_DISABLED",
                variant_code=inp.variant_code,
                module_code=module_code,
                currency=(inp.currency or ("INR" if country == "IN" else "USD")),
                pricebook_id=UUID("00000000-0000-0000-0000-000000000000"),
                pricebook_name="",
                total_credits=0,
                total_money="0",
                lines=[],
            )

        try:
            q = await quote_variant(
                conn,
                user_id=auth.user_id,
                variant_code=inp.variant_code,
                params=inp.params or {},
                channel=inp.channel,
                country_code=country,
                currency=inp.currency,
                billing_mode=gate.billing_mode,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        return QuoteOut(
            allowed=True,
            billing_mode=gate.billing_mode,
            reason=gate.reason or "",
            variant_code=q.variant_code,
            module_code=module_code,
            currency=q.currency,
            pricebook_id=q.pricebook_id,
            pricebook_name=q.pricebook_name,
            total_credits=q.total_credits,
            total_money=str(q.total_money),
            alt_currency=q.alt_currency,
            alt_total_money=str(q.alt_total_money) if q.alt_total_money is not None else None,
            lines=[
                QuoteLineOut(
                    sku_code=l.sku_code,
                    name=l.name,
                    category=l.category,
                    provider_hint=l.provider_hint,
                    unit=l.unit,
                    qty=str(l.qty),
                    unit_credits=l.unit_credits,
                    line_credits=l.line_credits,
                    unit_money=str(l.unit_money) if l.unit_money is not None else None,
                    line_money=str(l.line_money),
                )
                for l in q.lines
            ],
        )