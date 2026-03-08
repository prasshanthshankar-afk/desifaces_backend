# services/svc-pricing/app/app/api/routes/pricing.py
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import AuthContext, AuthDep, PoolDep
from app.services.engine.module_gate import evaluate_gate
from app.services.engine.pricing_engine import quote_variant

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


# -------------------------
# helpers (robust)
# -------------------------

def _as_dict_loose(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def _as_decimal(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _q_money(x: Decimal, money_decimals: int = 2) -> Decimal:
    q = Decimal("1").scaleb(-money_decimals)
    return x.quantize(q, rounding=ROUND_HALF_UP)


def _q_pct(x: Decimal) -> Decimal:
    q = Decimal("1").scaleb(-4)
    return x.quantize(q, rounding=ROUND_HALF_UP)


def _norm_currency(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    v = x.strip().upper()
    return v or None


async def _resolve_tier_code(conn, user_id: UUID) -> str:
    ent = await conn.fetchrow("select tier_code from pricing_user_entitlements where user_id=$1", user_id)
    if ent and ent.get("tier_code"):
        return str(ent["tier_code"])

    u = await conn.fetchrow("select tier from core.users where id=$1", user_id)
    if u and u.get("tier"):
        return str(u["tier"])

    return "free"


async def _usd_to_currency_rate(conn, currency: str) -> Optional[Decimal]:
    c = (currency or "").upper()
    if c == "USD":
        return Decimal("1")

    fx = await conn.fetchrow(
        """
        select rate
        from pricing_fx_rates
        where base_currency='USD' and quote_currency=$1
        order by as_of desc
        limit 1
        """,
        c,
    )
    if fx and fx.get("rate") is not None:
        r = _as_decimal(fx["rate"], "0")
        return r if r > 0 else None

    # best-effort fallback from credit_value ratios
    try:
        usd = await conn.fetchrow(
            """
            select money_per_credit
            from pricing_credit_value
            where currency='USD'
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc limit 1
            """
        )
        tgt = await conn.fetchrow(
            """
            select money_per_credit
            from pricing_credit_value
            where currency=$1
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc limit 1
            """,
            c,
        )
        if usd and tgt:
            usd_mpc = _as_decimal(usd["money_per_credit"], "0")
            tgt_mpc = _as_decimal(tgt["money_per_credit"], "0")
            if usd_mpc > 0:
                return tgt_mpc / usd_mpc
    except Exception:
        return None

    return None


async def _load_cost_components(conn, sku_codes: list[str]) -> dict:
    if not sku_codes:
        return {}
    rows = await conn.fetch(
        """
        select distinct on (sku_code, component_code)
          sku_code, component_code, cost_model, cost_currency,
          variable_cost_money, fixed_monthly_cost_money, assumed_monthly_units
        from pricing_sku_costs
        where sku_code = any($1::text[])
          and is_active = true
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by sku_code, component_code, effective_from desc
        """,
        sku_codes,
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(str(d["sku_code"]), []).append(d)
    return out


def _component_unit_cost_usd(comp: dict) -> Optional[Decimal]:
    cur = str(comp.get("cost_currency") or "USD").upper()
    if cur != "USD":
        return None
    model = str(comp.get("cost_model") or "blended").lower()
    var = _as_decimal(comp.get("variable_cost_money"), "0")
    fixed = _as_decimal(comp.get("fixed_monthly_cost_money"), "0")
    units = _as_decimal(comp.get("assumed_monthly_units"), "0")
    amort = (fixed / units) if units > 0 else Decimal("0")

    if model == "variable":
        return var
    if model == "amortized":
        return amort
    return var + amort


async def _compute_economics(conn, *, currency: str, revenue_money: Decimal, quote_lines: list[dict]) -> dict:
    skus: list[str] = []
    qty_by_sku: dict[str, Decimal] = {}

    for ln in quote_lines:
        sku = str(ln.get("sku_code") or "").strip()
        if not sku:
            continue
        q = _as_decimal(ln.get("qty"), "0")
        if q <= 0:
            continue
        skus.append(sku)
        qty_by_sku[sku] = qty_by_sku.get(sku, Decimal("0")) + q

    skus = sorted(set(skus))
    if not skus:
        return {"currency": currency, "has_costs_complete": False, "missing_cost_skus": [], "reason": "no_skus"}

    comps_by_sku = await _load_cost_components(conn, skus)

    missing: list[str] = []
    cogs_usd = Decimal("0")

    for sku in skus:
        comps = comps_by_sku.get(sku) or []
        if not comps:
            missing.append(sku)
            continue

        unit_cost_usd = Decimal("0")
        ok = False
        for comp in comps:
            c = _component_unit_cost_usd(comp)
            if c is None:
                continue
            ok = True
            unit_cost_usd += c

        if not ok:
            missing.append(sku)
            continue

        cogs_usd += unit_cost_usd * qty_by_sku.get(sku, Decimal("0"))

    rate = await _usd_to_currency_rate(conn, currency)
    if rate is None or rate <= 0:
        return {
            "currency": currency,
            "has_costs_complete": False,
            "missing_cost_skus": missing,
            "reason": "missing_fx_rate",
            "cogs_usd_partial": str(_q_money(cogs_usd)),
        }

    cogs_money = _q_money(cogs_usd * rate, 2)
    rev = _q_money(revenue_money, 2)

    has_complete = len(missing) == 0
    if not has_complete:
        return {
            "currency": currency,
            "has_costs_complete": False,
            "missing_cost_skus": missing,
            "reason": "missing_cost_rows",
            "revenue_money_est": str(rev),
            "cogs_money_est": None,
            "gross_margin_money_est": None,
            "gross_margin_pct_est": None,
        }

    gm = _q_money(rev - cogs_money, 2)
    pct = _q_pct(gm / rev) if rev > 0 else None

    return {
        "currency": currency,
        "has_costs_complete": True,
        "missing_cost_skus": [],
        "reason": "ok",
        "revenue_money_est": str(rev),
        "cogs_money_est": str(cogs_money),
        "gross_margin_money_est": str(gm),
        "gross_margin_pct_est": str(pct) if pct is not None else None,
    }


# -------------------------
# request/response models
# -------------------------

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

    # Would-have-cost transparency
    shadow_total_credits: Optional[int] = None
    shadow_total_money: Optional[str] = None

    # Economics estimate (internal; useful for dashboards/ops)
    economics: Dict[str, Any] = Field(default_factory=dict)

    alt_currency: Optional[str] = None
    alt_total_money: Optional[str] = None

    lines: list[QuoteLineOut]


@router.post("/quote", response_model=QuoteOut)
async def quote(inp: QuoteIn, auth: AuthContext = AuthDep, pool=PoolDep) -> QuoteOut:
    country = (inp.country_code or auth.country_code or "").upper()
    currency = _norm_currency(inp.currency)

    async with pool.acquire() as conn:
        v = await conn.fetchrow(
            "select category from pricing_variants where code = $1 and is_active=true",
            inp.variant_code,
        )
        if not v:
            raise HTTPException(status_code=404, detail="PRICING_UNKNOWN_OR_INACTIVE_VARIANT")

        category = str(v["category"])
        module_code = f"module.{category}"
        tier_code = await _resolve_tier_code(conn, auth.user_id)

        gate = await evaluate_gate(
            conn,
            module_code=module_code,
            channel=inp.channel,
            country_code=country,
            tier_code=tier_code,
        )

        # Always compute a quote for transparency (even if module disabled),
        # but if disabled, we return allowed=false and billing_mode=disabled.
        # If quote fails, we still return allowed=false with zeroed totals.
        q = None
        try:
            q = await quote_variant(
                conn,
                user_id=auth.user_id,
                variant_code=inp.variant_code,
                params=inp.params or {},
                channel=inp.channel,
                country_code=country,
                currency=currency,
                billing_mode=gate.billing_mode,
            )
        except ValueError as e:
            if not gate.allowed:
                return QuoteOut(
                    allowed=False,
                    billing_mode=gate.billing_mode,
                    reason=gate.reason or "MODULE_DISABLED",
                    variant_code=inp.variant_code,
                    module_code=module_code,
                    currency=(currency or ("INR" if country == "IN" else "USD")),
                    pricebook_id=UUID("00000000-0000-0000-0000-000000000000"),
                    pricebook_name="",
                    total_credits=0,
                    total_money="0",
                    shadow_total_credits=None,
                    shadow_total_money=None,
                    economics={"reason": f"quote_failed:{str(e)}"},
                    lines=[],
                )
            raise HTTPException(status_code=400, detail=str(e))

        # Economics estimate based on SHADOW revenue (what would have been billed)
        economics: Dict[str, Any] = {}
        try:
            shadow_money = _as_decimal(str(q.shadow_total_money or q.total_money), "0")
            economics = await _compute_economics(
                conn,
                currency=q.currency,
                revenue_money=shadow_money,
                quote_lines=[{"sku_code": l.sku_code, "qty": str(l.qty)} for l in q.lines],
            )
        except Exception:
            economics = {"currency": q.currency, "has_costs_complete": False, "reason": "economics_failed"}

        if not gate.allowed:
            # Show price transparency but block action
            return QuoteOut(
                allowed=False,
                billing_mode=gate.billing_mode,
                reason=gate.reason or "MODULE_DISABLED",
                variant_code=q.variant_code,
                module_code=module_code,
                currency=q.currency,
                pricebook_id=q.pricebook_id,
                pricebook_name=q.pricebook_name,
                total_credits=q.total_credits,
                total_money=str(q.total_money),
                shadow_total_credits=q.shadow_total_credits,
                shadow_total_money=str(q.shadow_total_money) if q.shadow_total_money is not None else None,
                economics=economics,
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
            shadow_total_credits=q.shadow_total_credits,
            shadow_total_money=str(q.shadow_total_money) if q.shadow_total_money is not None else None,
            economics=economics,
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