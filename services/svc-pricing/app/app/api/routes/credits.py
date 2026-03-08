# services/svc-pricing/app/app/api/routes/credits.py
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import AuthContext, AuthDep, PoolDep
from app.services.engine.module_gate import evaluate_gate
from app.services.reservations.reservation_service import (
    FinalizeReceipt,
    ReservationView,
    get_balance,
    reserve as reserve_impl,
    finalize as finalize_impl,
    release as release_impl,
)

router = APIRouter(prefix="/api/credits", tags=["credits"])


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


async def _resolve_tier_code(conn, user_id: UUID) -> str:
    """
    Tier resolution order:
      1) pricing_user_entitlements (supports future tiers like developer/api_enterprise)
      2) core.users.tier (free|pro|enterprise)
      3) free
    """
    ent = await conn.fetchrow(
        "select tier_code from pricing_user_entitlements where user_id=$1",
        user_id,
    )
    if ent and ent.get("tier_code"):
        return str(ent["tier_code"])

    u = await conn.fetchrow(
        "select tier from core.users where id=$1",
        user_id,
    )
    if u and u.get("tier"):
        return str(u["tier"])

    return "free"


def _safe_money_str(x: Any) -> str:
    if x is None:
        return "0"
    return str(x)


def _norm_currency(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    v = x.strip().upper()
    return v or None


# -------------------------
# models
# -------------------------

class BalanceOut(BaseModel):
    balance_credits: int
    reserved_credits: int
    available_credits: int


@router.get("/balance", response_model=BalanceOut)
async def balance(auth: AuthContext = AuthDep, pool=PoolDep) -> BalanceOut:
    async with pool.acquire() as conn:
        b = await get_balance(conn, auth.user_id)
        return BalanceOut(
            balance_credits=b.balance_credits,
            reserved_credits=b.reserved_credits,
            available_credits=b.available_credits,
        )


class ReserveIn(BaseModel):
    idempotency_key: str
    variant_code: str
    params: Dict[str, Any] = Field(default_factory=dict)
    channel: str = Field(default="web")
    currency: Optional[str] = None
    country_code: Optional[str] = None
    job_ref: Optional[str] = None
    ttl_seconds: Optional[int] = None


class ReserveOut(BaseModel):
    reservation_id: UUID
    status: str
    reserved_credits: int
    expires_at: str
    currency: str
    estimated_money: str
    billing_mode: str

    balance_credits: int
    reserved_credits_total: int
    available_credits: int

    # FULL transparency snapshot (what we stored)
    quote_breakdown: Dict[str, Any]


@router.post("/reserve", response_model=ReserveOut)
async def reserve(inp: ReserveIn, auth: AuthContext = AuthDep, pool=PoolDep) -> ReserveOut:
    country = (inp.country_code or auth.country_code or "").upper()
    currency = _norm_currency(inp.currency)

    async with pool.acquire() as conn:
        v = await conn.fetchrow(
            "select category from pricing_variants where code=$1 and is_active=true",
            inp.variant_code,
        )
        if not v:
            raise HTTPException(status_code=404, detail="PRICING_UNKNOWN_OR_INACTIVE_VARIANT")

        module_code = f"module.{str(v['category'])}"
        tier_code = await _resolve_tier_code(conn, auth.user_id)

        gate = await evaluate_gate(
            conn,
            module_code=module_code,
            channel=inp.channel,
            country_code=country,
            tier_code=tier_code,
        )

        if not gate.allowed:
            raise HTTPException(status_code=403, detail=f"PRICING_MODULE_DISABLED:{gate.reason}")

        try:
            rv: ReservationView = await reserve_impl(
                conn,
                user_id=auth.user_id,
                idempotency_key=inp.idempotency_key,
                variant_code=inp.variant_code,
                params=inp.params or {},
                channel=inp.channel,
                country_code=country,
                currency=currency,
                billing_mode=gate.billing_mode,
                job_ref=inp.job_ref,
                ttl_seconds=inp.ttl_seconds,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        b = await get_balance(conn, auth.user_id)

        # quote_breakdown is the stored quote_json snapshot (includes economics)
        qb = rv.quote if isinstance(rv.quote, dict) else _as_dict_loose(rv.quote)

        return ReserveOut(
            reservation_id=rv.reservation_id,
            status=rv.status,
            reserved_credits=rv.reserved_credits,
            expires_at=rv.expires_at.isoformat(),
            currency=str(rv.currency or currency or "USD"),
            estimated_money=_safe_money_str(rv.estimated_money),
            billing_mode=str(gate.billing_mode),
            balance_credits=b.balance_credits,
            reserved_credits_total=b.reserved_credits,
            available_credits=b.available_credits,
            quote_breakdown=qb,
        )


class FinalizeIn(BaseModel):
    reservation_id: UUID
    finalize_idempotency_key: str
    actuals: Dict[str, Any] = Field(default_factory=dict)  # future metering
    channel: str = Field(default="web")
    country_code: Optional[str] = None


class FinalizeOut(BaseModel):
    reservation_id: UUID
    status: str
    charged_credits: int
    charged_money: str

    balance_before: int
    reserved_before: int
    balance_after: int
    reserved_after: int
    available_after: int

    # FULL transparency snapshot after finalize (updated quote_json with economics_final)
    quote_breakdown: Dict[str, Any]

    # small summarized breakdown for UI convenience
    finalize_breakdown: Dict[str, Any]


@router.post("/finalize", response_model=FinalizeOut)
async def finalize(inp: FinalizeIn, auth: AuthContext = AuthDep, pool=PoolDep) -> FinalizeOut:
    country = (inp.country_code or auth.country_code or "").upper()

    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            select id, status, reserved_credits, currency, channel, country_code, quote_json
            from pricing_credit_reservations
            where user_id=$1 and id=$2
            """,
            auth.user_id,
            inp.reservation_id,
        )
        if not r:
            raise HTTPException(status_code=404, detail="PRICING_RESERVATION_NOT_FOUND")

        quote = _as_dict_loose(r["quote_json"])
        reserved_credits = int(r["reserved_credits"] or 0)

        # IMPORTANT precedence: billing_mode_snapshot (gate) > billing_mode (engine)
        snapshot_mode = (
            quote.get("billing_mode_snapshot")
            or quote.get("billing_mode")
            or quote.get("gate_billing_mode")
        )
        if snapshot_mode:
            billing_mode = str(snapshot_mode)
        else:
            # fallback inference only (should be rare)
            category = str(quote.get("category") or "")
            if not category:
                vc = str(quote.get("variant_code") or "")
                if vc:
                    v = await conn.fetchrow("select category from pricing_variants where code=$1", vc)
                    category = str(v["category"]) if v else ""
            module_code = f"module.{category}" if category else "module.unknown"

            tier_code = await _resolve_tier_code(conn, auth.user_id)
            gate = await evaluate_gate(
                conn,
                module_code=module_code,
                channel=inp.channel or str(r["channel"] or "web"),
                country_code=country or str(r["country_code"] or ""),
                tier_code=tier_code,
            )
            billing_mode = "bill" if reserved_credits > 0 else str(gate.billing_mode or "free")

        # Use stored defaults if caller changes/omits
        channel = inp.channel or str(r["channel"] or "web")
        country_final = country or str(r["country_code"] or "")

        try:
            receipt: FinalizeReceipt = await finalize_impl(
                conn,
                user_id=auth.user_id,
                reservation_id=inp.reservation_id,
                finalize_idempotency_key=inp.finalize_idempotency_key,
                actuals=inp.actuals or {},
                channel=channel,
                country_code=country_final,
                billing_mode=billing_mode,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Re-read the reservation to return the authoritative post-finalize snapshot
        r2 = await conn.fetchrow(
            """
            select quote_json, currency
            from pricing_credit_reservations
            where user_id=$1 and id=$2
            """,
            auth.user_id,
            inp.reservation_id,
        )
        qb = _as_dict_loose(r2["quote_json"]) if r2 else quote

        finalize_breakdown = {
            "billing_mode_effective": billing_mode,
            "currency": str(r2["currency"] if r2 else (r["currency"] or "")),
            "charged_credits": receipt.charged_credits,
            "charged_money": _safe_money_str(receipt.charged_money),
            "economics": _as_dict_loose(qb.get("economics")),
        }

        return FinalizeOut(
            reservation_id=receipt.reservation_id,
            status=receipt.status,
            charged_credits=receipt.charged_credits,
            charged_money=_safe_money_str(receipt.charged_money),
            balance_before=receipt.balance_before,
            reserved_before=receipt.reserved_before,
            balance_after=receipt.balance_after,
            reserved_after=receipt.reserved_after,
            available_after=receipt.available_after,
            quote_breakdown=qb,
            finalize_breakdown=finalize_breakdown,
        )


class ReleaseIn(BaseModel):
    reservation_id: Optional[UUID] = None
    idempotency_key: Optional[str] = None
    channel: str = Field(default="web")
    country_code: Optional[str] = None
    reason: str = Field(default="user_cancel")


class ReleaseOut(BaseModel):
    reservation_id: UUID
    status: str
    reserved_credits: int

    # include stored snapshot for transparency (optional but helpful)
    quote_breakdown: Dict[str, Any]


@router.post("/release", response_model=ReleaseOut)
async def release(inp: ReleaseIn, auth: AuthContext = AuthDep, pool=PoolDep) -> ReleaseOut:
    country = (inp.country_code or auth.country_code or "").upper()

    async with pool.acquire() as conn:
        try:
            rv = await release_impl(
                conn,
                user_id=auth.user_id,
                reservation_id=inp.reservation_id,
                idempotency_key=inp.idempotency_key,
                channel=inp.channel,
                country_code=country,
                reason=inp.reason,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        qb = rv.quote if isinstance(rv.quote, dict) else _as_dict_loose(rv.quote)

        return ReleaseOut(
            reservation_id=rv.reservation_id,
            status=rv.status,
            reserved_credits=rv.reserved_credits,
            quote_breakdown=qb,
        )