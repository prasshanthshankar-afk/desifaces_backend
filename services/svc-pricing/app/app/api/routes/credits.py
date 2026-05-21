from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import AuthContext, AuthDep, PoolDep
from app.services.entitlement_service import resolve_entitlement
from app.services.reservations.reservation_service import (
    FinalizeReceipt,
    ReservationView,
    finalize as finalize_impl,
    get_balance,
    release as release_impl,
    reserve as reserve_impl,
)

router = APIRouter(prefix="/api/credits", tags=["credits"])


# -------------------------
# helpers
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


def _safe_money_str(x: Any) -> str:
    if x is None:
        return "0"
    return str(x)


def _norm_currency(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    v = x.strip().upper()
    return v or None


def _clean_text(x: Any, *, default: str = "") -> str:
    if x is None:
        return default
    s = str(x).strip()
    return s if s else default


def _norm_country(x: Optional[str]) -> str:
    return _clean_text(x).upper()


def _normalize_settlement_mode(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in {"postpaid", "invoice", "bill", "billed", "money"}:
        return "postpaid"
    if s in {"prepaid", "credit", "credits", "wallet", "payg"}:
        return "prepaid"
    if s in {"hybrid", "mixed"}:
        return "hybrid"
    return ""


def _as_uuid_or_none(x: Any) -> Optional[UUID]:
    if x is None:
        return None
    try:
        s = str(x).strip()
        if not s:
            return None
        return UUID(s)
    except Exception:
        return None


async def _table_exists(conn, regclass_name: str) -> bool:
    try:
        row = await conn.fetchrow("select to_regclass($1) as reg", regclass_name)
        return bool(row and row.get("reg"))
    except Exception:
        return False


async def _resolve_billing_account_context(conn, user_id: UUID) -> Dict[str, Any]:
    out = {
        "billing_account_id": None,
        "billing_account_code": f"user:{user_id}",
        "billing_account_type": "individual",
        "account_billing_mode": "prepaid",
        "default_currency": "USD",
        "source": "implicit_user_default",
    }

    try:
        if await _table_exists(conn, "public.pricing_billing_account_members") and await _table_exists(
            conn, "public.pricing_billing_accounts"
        ):
            row = await conn.fetchrow(
                """
                select
                  ba.id,
                  ba.account_code,
                  ba.account_type,
                  ba.billing_mode,
                  ba.default_currency
                from public.pricing_billing_account_members bam
                join public.pricing_billing_accounts ba
                  on ba.id = bam.billing_account_id
                where bam.user_id = $1
                  and bam.status = 'active'
                  and ba.status = 'active'
                order by
                  bam.is_default desc,
                  case bam.role
                    when 'owner' then 0
                    when 'finance_admin' then 1
                    when 'member' then 2
                    else 3
                  end,
                  bam.created_at asc
                limit 1
                """,
                user_id,
            )
            if row:
                out.update(
                    {
                        "billing_account_id": str(row["id"]),
                        "billing_account_code": str(row["account_code"] or out["billing_account_code"]),
                        "billing_account_type": str(row["account_type"] or "individual"),
                        "account_billing_mode": _normalize_settlement_mode(row["billing_mode"]) or "prepaid",
                        "default_currency": str(row["default_currency"] or "USD"),
                        "source": "billing_account_members",
                    }
                )
                return out
    except Exception:
        pass

    try:
        if await _table_exists(conn, "public.pricing_credit_accounts") and await _table_exists(
            conn, "public.pricing_billing_accounts"
        ):
            row = await conn.fetchrow(
                """
                select
                  ba.id,
                  ba.account_code,
                  ba.account_type,
                  ba.billing_mode,
                  ba.default_currency
                from public.pricing_credit_accounts pca
                join public.pricing_billing_accounts ba
                  on ba.id = pca.billing_account_id
                where pca.user_id = $1
                limit 1
                """,
                user_id,
            )
            if row:
                out.update(
                    {
                        "billing_account_id": str(row["id"]),
                        "billing_account_code": str(row["account_code"] or out["billing_account_code"]),
                        "billing_account_type": str(row["account_type"] or "individual"),
                        "account_billing_mode": _normalize_settlement_mode(row["billing_mode"]) or "prepaid",
                        "default_currency": str(row["default_currency"] or "USD"),
                        "source": "pricing_credit_accounts.billing_account_id",
                    }
                )
                return out
    except Exception:
        pass

    try:
        if await _table_exists(conn, "public.pricing_billing_accounts"):
            row = await conn.fetchrow(
                """
                select id, account_code, account_type, billing_mode, default_currency
                from public.pricing_billing_accounts
                where account_code = $1
                  and status = 'active'
                limit 1
                """,
                f"user:{user_id}",
            )
            if row:
                out.update(
                    {
                        "billing_account_id": str(row["id"]),
                        "billing_account_code": str(row["account_code"] or out["billing_account_code"]),
                        "billing_account_type": str(row["account_type"] or "individual"),
                        "account_billing_mode": _normalize_settlement_mode(row["billing_mode"]) or "prepaid",
                        "default_currency": str(row["default_currency"] or "USD"),
                        "source": "billing_accounts.user_code",
                    }
                )
                return out
    except Exception:
        pass

    return out


def _resolve_effective_settlement_mode(
    *,
    account_billing_mode: str,
    entitlement_billing_mode: str,
    meta: Dict[str, Any],
) -> str:
    acct_mode = _normalize_settlement_mode(account_billing_mode) or "prepaid"
    requested = _normalize_settlement_mode(
        meta.get("settlement_mode")
        or meta.get("preferred_settlement_mode")
        or meta.get("settlement_mode_hint")
    )

    if acct_mode in {"prepaid", "postpaid"}:
        return acct_mode

    if requested in {"prepaid", "postpaid", "hybrid"}:
        return requested

    entitlement_mode = str(entitlement_billing_mode or "").strip().lower()
    if entitlement_mode in {"included", "free", "shadow", "disabled"}:
        return "prepaid"

    return "postpaid"


def _derive_service_name(category: str, params: Dict[str, Any]) -> str:
    explicit = _clean_text(params.get("service_name") or params.get("caller_service_name"))
    if explicit:
        return explicit
    mapping = {
        "face": "svc-face",
        "audio": "svc-audio",
        "fusion": "svc-fusion",
        "commerce": "svc-commerce",
        "music": "svc-music",
        "marketing": "svc-marketing",
    }
    return mapping.get(category, f"svc-{category}" if category else "svc-pricing")


def _derive_service_action(params: Dict[str, Any]) -> str:
    return _clean_text(params.get("service_action"), default="generate")


async def _fetch_reservation_row(conn, user_id: UUID, reservation_id: UUID):
    try:
        return await conn.fetchrow(
            """
            select
              id,
              user_id,
              status,
              reserved_credits,
              expires_at,
              currency,
              estimated_money,
              channel,
              country_code,
              billing_account_id,
              settlement_mode,
              service_name,
              service_action,
              sku_code,
              quote_json
            from pricing_credit_reservations
            where user_id=$1 and id=$2
            """,
            user_id,
            reservation_id,
        )
    except Exception:
        return await conn.fetchrow(
            """
            select
              id,
              user_id,
              status,
              reserved_credits,
              expires_at,
              currency,
              estimated_money,
              channel,
              country_code,
              quote_json
            from pricing_credit_reservations
            where user_id=$1 and id=$2
            """,
            user_id,
            reservation_id,
        )


async def _patch_quote_json(
    conn,
    *,
    reservation_id: UUID,
    user_id: UUID,
    patch: Dict[str, Any],
) -> None:
    clean_patch = {k: v for k, v in (patch or {}).items() if v is not None}
    if not clean_patch:
        return

    row = await conn.fetchrow(
        """
        select quote_json
        from pricing_credit_reservations
        where id = $1 and user_id = $2
        """,
        reservation_id,
        user_id,
    )
    if not row:
        return

    current = _as_dict_loose(row["quote_json"])
    current.update(clean_patch)

    await conn.execute(
        """
        update pricing_credit_reservations
        set quote_json = $3::jsonb,
            updated_at = now()
        where id = $1 and user_id = $2
        """,
        reservation_id,
        user_id,
        current,
    )


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
    pricing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    entitlement_reason: Optional[str] = None
    tier_code: Optional[str] = None
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None

    balance_credits: int
    reserved_credits_total: int
    available_credits: int

    quote_breakdown: Dict[str, Any]


@router.post("/reserve", response_model=ReserveOut)
async def reserve(inp: ReserveIn, auth: AuthContext = AuthDep, pool=PoolDep) -> ReserveOut:
    country = _norm_country(inp.country_code or auth.country_code)

    async with pool.acquire() as conn:
        v = await conn.fetchrow(
            "select category from pricing_variants where code=$1 and is_active=true",
            inp.variant_code,
        )
        if not v:
            raise HTTPException(status_code=404, detail="PRICING_UNKNOWN_OR_INACTIVE_VARIANT")

        category = str(v["category"])
        params = dict(inp.params or {})
        service_name = _derive_service_name(category, params)
        service_action = _derive_service_action(params)

        resolved = await resolve_entitlement(
            conn,
            user_id=auth.user_id,
            service_name=service_name,
            service_action=service_action,
            sku_code=inp.variant_code,
            channel=inp.channel,
            country_code=country,
        )
        if resolved.reason == "PRICING_UNKNOWN_OR_INACTIVE_VARIANT":
            raise HTTPException(status_code=404, detail=resolved.reason)
        if not resolved.allowed:
            raise HTTPException(status_code=403, detail=resolved.reason or "ENTITLEMENT_BLOCKED")

        billing_ctx = await _resolve_billing_account_context(conn, auth.user_id)
        settlement_mode = _resolve_effective_settlement_mode(
            account_billing_mode=str(billing_ctx.get("account_billing_mode") or "prepaid"),
            entitlement_billing_mode=str(resolved.billing_mode or ""),
            meta={**params, **dict(resolved.meta or {})},
        )
        currency = _norm_currency(inp.currency) or str(billing_ctx.get("default_currency") or "USD")

        try:
            rv: ReservationView = await reserve_impl(
                conn,
                user_id=auth.user_id,
                idempotency_key=inp.idempotency_key,
                variant_code=inp.variant_code,
                params={
                    **params,
                    "service_name": service_name,
                    "service_action": service_action,
                },
                channel=inp.channel,
                country_code=country,
                currency=currency,
                pricing_mode=resolved.pricing_mode,
                billing_mode_snapshot=resolved.billing_mode,
                job_ref=inp.job_ref,
                ttl_seconds=inp.ttl_seconds,
                entitlement_source=resolved.source,
                entitlement_reason=resolved.reason,
                tier_code=resolved.tier_code,
                billing_account_id=_as_uuid_or_none(billing_ctx.get("billing_account_id")),
                settlement_mode=settlement_mode,
                service_name=service_name,
                service_action=service_action,
                sku_code=inp.variant_code,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await _patch_quote_json(
            conn,
            reservation_id=rv.reservation_id,
            user_id=auth.user_id,
            patch={
                "service_name": service_name,
                "service_action": service_action,
                "billing_mode_snapshot": resolved.billing_mode,
                "pricing_mode_used": resolved.pricing_mode,
                "entitlement_source": resolved.source,
                "entitlement_reason": resolved.reason,
                "tier_code": resolved.tier_code,
                "billing_account_id": billing_ctx.get("billing_account_id"),
                "billing_account_code": billing_ctx.get("billing_account_code"),
                "billing_account_type": billing_ctx.get("billing_account_type"),
                "account_billing_mode": billing_ctx.get("account_billing_mode"),
                "settlement_mode": settlement_mode,
            },
        )

        b = await get_balance(conn, auth.user_id)
        qb = rv.quote if isinstance(rv.quote, dict) else _as_dict_loose(rv.quote)

        return ReserveOut(
            reservation_id=rv.reservation_id,
            status=rv.status,
            reserved_credits=rv.reserved_credits,
            expires_at=rv.expires_at.isoformat(),
            currency=str(rv.currency or currency or "USD"),
            estimated_money=_safe_money_str(rv.estimated_money),
            billing_mode=str(resolved.billing_mode),
            pricing_mode=str(resolved.pricing_mode),
            entitlement_source=str(resolved.source),
            entitlement_reason=str(resolved.reason) if resolved.reason else None,
            tier_code=str(resolved.tier_code) if resolved.tier_code else None,
            billing_account_id=str(billing_ctx.get("billing_account_id") or "") or None,
            settlement_mode=settlement_mode or None,
            balance_credits=b.balance_credits,
            reserved_credits_total=b.reserved_credits,
            available_credits=b.available_credits,
            quote_breakdown=qb,
        )


class FinalizeIn(BaseModel):
    reservation_id: UUID
    finalize_idempotency_key: str
    actuals: Dict[str, Any] = Field(default_factory=dict)
    channel: str = Field(default="web")
    country_code: Optional[str] = None


class FinalizeOut(BaseModel):
    reservation_id: UUID
    status: str
    charged_credits: int
    charged_money: str
    billing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None

    balance_before: int
    reserved_before: int
    balance_after: int
    reserved_after: int
    available_after: int

    quote_breakdown: Dict[str, Any]
    finalize_breakdown: Dict[str, Any]


@router.post("/finalize", response_model=FinalizeOut)
async def finalize(inp: FinalizeIn, auth: AuthContext = AuthDep, pool=PoolDep) -> FinalizeOut:
    country = _norm_country(inp.country_code or auth.country_code)

    async with pool.acquire() as conn:
        r = await _fetch_reservation_row(conn, auth.user_id, inp.reservation_id)
        if not r:
            raise HTTPException(status_code=404, detail="PRICING_RESERVATION_NOT_FOUND")

        quote = _as_dict_loose(r["quote_json"])
        reserved_credits = int(r["reserved_credits"] or 0)

        service_name = _clean_text(r.get("service_name") if hasattr(r, "get") else None) or _clean_text(quote.get("service_name"))
        service_action = _clean_text(r.get("service_action") if hasattr(r, "get") else None) or _clean_text(quote.get("service_action"), default="generate")
        sku_code = _clean_text(r.get("sku_code") if hasattr(r, "get") else None) or _clean_text(quote.get("sku_code") or quote.get("variant_code"))

        snapshot_mode = (
            quote.get("billing_mode_snapshot")
            or quote.get("billing_mode")
            or quote.get("gate_billing_mode")
        )
        resolved = None
        if snapshot_mode:
            billing_mode = str(snapshot_mode)
        else:
            if not sku_code:
                raise HTTPException(status_code=400, detail="PRICING_VARIANT_CODE_MISSING")
            resolved = await resolve_entitlement(
                conn,
                user_id=auth.user_id,
                service_name=service_name or "svc-pricing",
                service_action=service_action or "generate",
                sku_code=sku_code,
                channel=inp.channel or str(r["channel"] or "web"),
                country_code=country or str(r["country_code"] or ""),
            )
            if not resolved.allowed:
                raise HTTPException(status_code=403, detail=resolved.reason or "ENTITLEMENT_BLOCKED")
            billing_mode = str(resolved.billing_mode)

        billing_account_id = _clean_text((r.get("billing_account_id") if hasattr(r, "get") else None) or quote.get("billing_account_id"))
        settlement_mode = _clean_text((r.get("settlement_mode") if hasattr(r, "get") else None) or quote.get("settlement_mode"))
        if not billing_account_id or not settlement_mode:
            billing_ctx = await _resolve_billing_account_context(conn, auth.user_id)
            if not billing_account_id:
                billing_account_id = _clean_text(billing_ctx.get("billing_account_id"))
            if not settlement_mode:
                settlement_mode = _resolve_effective_settlement_mode(
                    account_billing_mode=str(billing_ctx.get("account_billing_mode") or "prepaid"),
                    entitlement_billing_mode=str(billing_mode or ""),
                    meta={**inp.actuals, **dict((resolved.meta if resolved else {}) or {})},
                )

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
                billing_account_id=_as_uuid_or_none(billing_account_id),
                settlement_mode=settlement_mode,
                service_name=service_name or None,
                service_action=service_action or None,
                sku_code=sku_code or None,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        r2 = await _fetch_reservation_row(conn, auth.user_id, inp.reservation_id)
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
            billing_mode=billing_mode,
            entitlement_source=_clean_text(qb.get("entitlement_source")) or None,
            billing_account_id=billing_account_id or None,
            settlement_mode=settlement_mode or None,
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
    billing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None
    quote_breakdown: Dict[str, Any]


@router.post("/release", response_model=ReleaseOut)
async def release(inp: ReleaseIn, auth: AuthContext = AuthDep, pool=PoolDep) -> ReleaseOut:
    country = _norm_country(inp.country_code or auth.country_code)

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
            billing_mode=_clean_text(qb.get("billing_mode_snapshot")) or None,
            entitlement_source=_clean_text(qb.get("entitlement_source")) or None,
            billing_account_id=_clean_text(qb.get("billing_account_id")) or None,
            settlement_mode=_clean_text(qb.get("settlement_mode")) or None,
            quote_breakdown=qb,
        )
