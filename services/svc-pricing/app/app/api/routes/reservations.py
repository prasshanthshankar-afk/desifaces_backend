from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Sequence
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import PoolDep
from app.services.entitlement_service import resolve_entitlement
from app.services.reservations.reservation_service import (
    FinalizeReceipt,
    ReservationView,
    finalize as finalize_impl,
    release as release_impl,
    reserve as reserve_impl,
)

router = APIRouter(tags=["pricing-reservations"])


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def _as_dict_loose(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}

    if isinstance(x, dict):
        return x

    if isinstance(x, (list, tuple)):
        merged: Dict[str, Any] = {}
        for item in x:
            if isinstance(item, dict):
                merged.update(item)
                continue
            if isinstance(item, str):
                s = item.strip()
                if not s:
                    continue
                try:
                    v = json.loads(s)
                except Exception:
                    continue
                if isinstance(v, dict):
                    merged.update(v)
                elif isinstance(v, (list, tuple)):
                    merged.update(_as_dict_loose(v))
        return merged

    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
        except Exception:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, (list, tuple)):
            return _as_dict_loose(v)
        return {}

    try:
        return dict(x)
    except Exception:
        return {}


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(Decimal(str(value)))
    except Exception:
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off", ""}:
        return False
    return default


def _to_units_str(value: Any, default: str = "0") -> str:
    d = _to_decimal(value, default=default)
    if d == d.to_integral():
        return str(int(d))
    return format(d.normalize(), "f")


def _safe_money_str(x: Any) -> str:
    if x is None or x == "":
        return "0.00"
    try:
        return format(Decimal(str(x)).quantize(Decimal("0.01")), "f")
    except Exception:
        return str(x)


def _clean_bearer(token: str) -> str:
    value = (token or "").strip()
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _norm_currency(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    v = str(x).strip().upper()
    return v or None


def _norm_country(x: Optional[str]) -> str:
    if not x:
        return ""
    return str(x).strip().upper()


def _first_non_empty(*vals: Any) -> str:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _nested_get(d: Dict[str, Any], path: Sequence[str]) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _quote_pick(d: Dict[str, Any], *paths: Sequence[str]) -> str:
    for p in paths:
        v = _nested_get(d, p)
        s = _first_non_empty(v)
        if s:
            return s
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


def _normalize_settlement_mode(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in {"postpaid", "invoice", "bill", "billed"}:
        return "postpaid"
    if s in {"prepaid", "credit", "credits", "wallet", "payg"}:
        return "prepaid"
    if s in {"hybrid", "mixed"}:
        return "hybrid"
    return ""


async def _table_exists(conn, regclass_name: str) -> bool:
    try:
        row = await conn.fetchrow("select to_regclass($1) as reg", regclass_name)
        return bool(row and row.get("reg"))
    except Exception:
        return False


async def _resolve_tier_code(conn, user_id: UUID) -> str:
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
        meta.get("settlement_mode") or meta.get("preferred_settlement_mode")
    )

    if acct_mode in {"prepaid", "postpaid"}:
        return acct_mode

    if requested in {"prepaid", "postpaid"}:
        return requested

    entitlement_mode = str(entitlement_billing_mode or "").strip().lower()
    if entitlement_mode in {"included", "free", "shadow", "disabled"}:
        return "prepaid"

    return "postpaid"


def _studio_job_uuid_or_none(external_ref_type: str, external_ref_id: str) -> Optional[UUID]:
    if str(external_ref_type or "").strip().lower() != "studio_job":
        return None
    return _as_uuid_or_none(external_ref_id)


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


async def _lookup_ledger_event_id(conn, user_id: UUID, *idempotency_keys: str) -> Optional[str]:
    keys = [k for k in idempotency_keys if k]
    if not keys:
        return None
    row = await conn.fetchrow(
        """
        select id
        from pricing_credit_ledger_events
        where user_id = $1
          and idempotency_key = any($2::text[])
        order by created_at desc
        limit 1
        """,
        user_id,
        keys,
    )
    return str(row["id"]) if row else None


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


# ---------------------------------------------------------------------
# internal caller auth
# ---------------------------------------------------------------------
class PricingCallerContext(BaseModel):
    user_id: str
    service_name: str
    auth_mode: str = "internal_bearer"


def _require_internal_pricing_caller(
    authorization: Optional[str],
    x_user_id: Optional[str],
    x_service_name: Optional[str],
) -> PricingCallerContext:
    expected = _clean_bearer(
        os.getenv("DF_PRICING_INTERNAL_BEARER")
        or os.getenv("SVC_TO_SVC_BEARER")
        or ""
    )
    provided = _clean_bearer(authorization or "")

    if not expected:
        raise HTTPException(status_code=500, detail="pricing_internal_auth_misconfigured")

    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="unauthorized")

    user_id = (x_user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="missing_x_user_id")

    service_name = (x_service_name or "").strip() or "unknown"

    allowed = {
        s.strip()
        for s in (
            os.getenv(
                "DF_PRICING_ALLOWED_SERVICES",
                "svc-face,svc-commerce,svc-music,svc-marketing",
            )
        ).split(",")
        if s.strip()
    }
    if allowed and service_name not in allowed:
        raise HTTPException(status_code=403, detail="service_not_allowed")

    try:
        UUID(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_x_user_id")

    return PricingCallerContext(
        user_id=user_id,
        service_name=service_name,
        auth_mode="internal_bearer",
    )


# ---------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------
class PricingReserveRequest(BaseModel):
    user_id: str
    service_name: str
    service_action: str
    sku_code: str
    units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReserveResponse(BaseModel):
    status: str = "reserved"
    reservation_id: Optional[str] = None
    quote_id: Optional[str] = None
    reserved_units: Optional[str] = None
    billed_units: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None
    billing_mode: Optional[str] = None
    pricing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    entitlement_reason: Optional[str] = None
    tier_code: Optional[str] = None
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None
    message: Optional[str] = None


class PricingCommitRequest(BaseModel):
    user_id: str
    reservation_id: str
    actual_units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingCommitResponse(BaseModel):
    status: str = "committed"
    reservation_id: Optional[str] = None
    billed_units: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None
    billing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None
    message: Optional[str] = None


class PricingReleaseRequest(BaseModel):
    user_id: str
    reservation_id: str
    reason: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReleaseResponse(BaseModel):
    status: str = "released"
    reservation_id: Optional[str] = None
    released_units: Optional[str] = None
    billing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None
    message: Optional[str] = None


class ReservationOut(BaseModel):
    reservation_id: UUID
    status: str
    expires_at: str
    reserved_credits: int
    currency: str
    estimated_money: str = "0.00"
    channel: str = ""
    country_code: str = ""
    variant_code: str = ""
    category: str = ""
    billing_mode_snapshot: str = ""
    entitlement_source: str = ""
    hold_applied: bool = False
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None
    quote_breakdown: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------
# reserve -> real reserve_impl
# ---------------------------------------------------------------------
@router.post("/api/pricing/reservations/reserve", response_model=PricingReserveResponse)
async def reserve_reservation(
    req: PricingReserveRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> PricingReserveResponse:
    caller = _require_internal_pricing_caller(authorization, x_user_id, x_service_name)

    if str(req.user_id) != str(caller.user_id):
        raise HTTPException(status_code=403, detail="user_mismatch")

    user_uuid = UUID(str(req.user_id))
    meta = _as_dict_loose(req.meta)
    channel = _first_non_empty(meta.get("channel"), "service")
    country_code = _norm_country(meta.get("country_code"))
    currency = _norm_currency(meta.get("currency"))

    params = {
        **meta,
        "service_name": req.service_name,
        "service_action": req.service_action,
        "external_ref_type": req.external_ref_type,
        "external_ref_id": req.external_ref_id,
        "requested_units": _to_units_str(req.units, "1"),
        "variant_count": _to_int(req.units, 1),
        "units": _to_units_str(req.units, "1"),
        "caller_service_name": caller.service_name,
    }

    async with pool.acquire() as conn:
        resolved = await resolve_entitlement(
            conn,
            user_id=user_uuid,
            service_name=req.service_name,
            service_action=req.service_action,
            sku_code=req.sku_code,
            channel=channel,
            country_code=country_code,
        )
        if resolved.reason == "PRICING_UNKNOWN_OR_INACTIVE_VARIANT":
            raise HTTPException(status_code=404, detail=resolved.reason)
        if not resolved.allowed:
            raise HTTPException(status_code=403, detail=resolved.reason or "ENTITLEMENT_BLOCKED")

        billing_ctx = await _resolve_billing_account_context(conn, user_uuid)
        settlement_mode = _resolve_effective_settlement_mode(
            account_billing_mode=str(billing_ctx.get("account_billing_mode") or "prepaid"),
            entitlement_billing_mode=str(resolved.billing_mode or ""),
            meta=meta,
        )

        try:
            rv: ReservationView = await reserve_impl(
                conn,
                user_id=user_uuid,
                idempotency_key=req.idempotency_key,
                variant_code=req.sku_code,
                params=params,
                channel=channel,
                country_code=country_code,
                currency=currency or billing_ctx.get("default_currency"),
                pricing_mode=resolved.pricing_mode,
                billing_mode_snapshot=resolved.billing_mode,
                job_ref=req.external_ref_id,
                ttl_seconds=_to_int(meta.get("ttl_seconds"), 0) or None,
                entitlement_source=resolved.source,
                entitlement_reason=resolved.reason,
                tier_code=resolved.tier_code,
                billing_account_id=_as_uuid_or_none(billing_ctx.get("billing_account_id")),
                settlement_mode=settlement_mode,
                service_name=req.service_name,
                service_action=req.service_action,
                sku_code=req.sku_code,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await _patch_quote_json(
            conn,
            reservation_id=rv.reservation_id,
            user_id=user_uuid,
            patch={
                "service_name": req.service_name,
                "service_action": req.service_action,
                "external_ref_type": req.external_ref_type,
                "external_ref_id": req.external_ref_id,
                "internal_reserve_idempotency_key": req.idempotency_key,
                "caller_service_name": caller.service_name,
                "requested_units": _to_units_str(req.units, "1"),
                "sku_code": req.sku_code,
                "variant_code": req.sku_code,
                "channel": channel,
                "country_code": country_code,
                "currency": str(rv.currency or currency or "USD"),
                "billing_mode_snapshot": resolved.billing_mode,
                "pricing_mode_used": resolved.pricing_mode,
                "entitlement_source": resolved.source,
                "entitlement_reason": resolved.reason,
                "tier_code": resolved.tier_code,
                "module_code": resolved.module_code,
                "category": resolved.category,
                "billing_account_id": billing_ctx.get("billing_account_id"),
                "billing_account_code": billing_ctx.get("billing_account_code"),
                "billing_account_type": billing_ctx.get("billing_account_type"),
                "account_billing_mode": billing_ctx.get("account_billing_mode"),
                "settlement_mode": settlement_mode,
            },
        )

        row = await _fetch_reservation_row(conn, user_uuid, rv.reservation_id)
        qb = _as_dict_loose(row["quote_json"]) if row else _as_dict_loose(getattr(rv, "quote", {}))

    return PricingReserveResponse(
        status="reserved",
        reservation_id=str(rv.reservation_id),
        quote_id=_quote_pick(
            qb,
            ("quote_id",),
            ("economics", "quote_id"),
            ("economics_final", "quote_id"),
        )
        or None,
        reserved_units=_to_units_str(
            getattr(rv, "reserved_credits", None),
            "0",
        ),
        amount=_safe_money_str(getattr(rv, "estimated_money", None)),
        currency=_first_non_empty(getattr(rv, "currency", None), currency, "USD") or None,
        billing_mode=_first_non_empty(qb.get("billing_mode_snapshot"), "included") or None,
        pricing_mode=_first_non_empty(qb.get("pricing_mode_used")) or None,
        entitlement_source=_first_non_empty(qb.get("entitlement_source")) or None,
        entitlement_reason=_first_non_empty(qb.get("entitlement_reason")) or None,
        tier_code=_first_non_empty(qb.get("tier_code")) or None,
        billing_account_id=_first_non_empty(qb.get("billing_account_id")) or None,
        settlement_mode=_first_non_empty(qb.get("settlement_mode")) or None,
        message="Reservation created",
    )


# ---------------------------------------------------------------------
# commit -> real finalize_impl
# ---------------------------------------------------------------------
@router.post("/api/pricing/reservations/commit", response_model=PricingCommitResponse)
async def commit_reservation(
    req: PricingCommitRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> PricingCommitResponse:
    caller = _require_internal_pricing_caller(authorization, x_user_id, x_service_name)

    if str(req.user_id) != str(caller.user_id):
        raise HTTPException(status_code=403, detail="user_mismatch")

    user_uuid = UUID(str(req.user_id))
    reservation_uuid = UUID(str(req.reservation_id))
    meta = _as_dict_loose(req.meta)

    async with pool.acquire() as conn:
        row = await _fetch_reservation_row(conn, user_uuid, reservation_uuid)
        if not row:
            raise HTTPException(status_code=404, detail="PRICING_RESERVATION_NOT_FOUND")

        quote = _as_dict_loose(row["quote_json"])
        channel = _first_non_empty(meta.get("channel"), row["channel"], "service")
        country_final = _norm_country(_first_non_empty(meta.get("country_code"), row["country_code"]))

        snapshot_mode = (
            quote.get("billing_mode_snapshot")
            or quote.get("entitlement_mode")
            or quote.get("billing_mode")
            or quote.get("gate_billing_mode")
        )
        entitlement_source = _first_non_empty(quote.get("entitlement_source"))
        if snapshot_mode:
            billing_mode = str(snapshot_mode)
        else:
            variant_code = _first_non_empty(
                quote.get("variant_code"),
                quote.get("sku_code"),
                meta.get("variant_code"),
                meta.get("sku_code"),
            )
            service_name = _first_non_empty(quote.get("service_name"), meta.get("service_name"), caller.service_name)
            service_action = _first_non_empty(quote.get("service_action"), meta.get("service_action"))
            if not variant_code:
                raise HTTPException(status_code=400, detail="PRICING_VARIANT_CODE_MISSING")
            if not service_action:
                billing_mode = "bill"
                entitlement_source = entitlement_source or "legacy_default"
            else:
                resolved = await resolve_entitlement(
                    conn,
                    user_id=user_uuid,
                    service_name=service_name,
                    service_action=service_action,
                    sku_code=variant_code,
                    channel=channel,
                    country_code=country_final,
                )
                if not resolved.allowed:
                    raise HTTPException(status_code=403, detail=resolved.reason or "ENTITLEMENT_BLOCKED")
                billing_mode = resolved.billing_mode
                entitlement_source = resolved.source

        billing_account_id = _first_non_empty(
            getattr(row, "get", lambda *_: None)("billing_account_id") if row else None,
            quote.get("billing_account_id"),
        )
        settlement_mode = _first_non_empty(
            getattr(row, "get", lambda *_: None)("settlement_mode") if row else None,
            quote.get("settlement_mode"),
        )

        if not billing_account_id or not settlement_mode:
            billing_ctx = await _resolve_billing_account_context(conn, user_uuid)
            if not billing_account_id:
                billing_account_id = _first_non_empty(billing_ctx.get("billing_account_id"))
            if not settlement_mode:
                settlement_mode = _resolve_effective_settlement_mode(
                    account_billing_mode=str(billing_ctx.get("account_billing_mode") or "prepaid"),
                    entitlement_billing_mode=str(billing_mode or ""),
                    meta=meta,
                )

        service_name_final = _first_non_empty(
            getattr(row, "get", lambda *_: None)("service_name") if row else None,
            quote.get("service_name"),
            meta.get("service_name"),
            caller.service_name,
        )
        service_action_final = _first_non_empty(
            getattr(row, "get", lambda *_: None)("service_action") if row else None,
            quote.get("service_action"),
            meta.get("service_action"),
        )
        sku_code_final = _first_non_empty(
            getattr(row, "get", lambda *_: None)("sku_code") if row else None,
            quote.get("sku_code"),
            quote.get("variant_code"),
            meta.get("sku_code"),
            meta.get("variant_code"),
        )

        actual_units = _to_units_str(req.actual_units, "1")

        try:
            receipt: FinalizeReceipt = await finalize_impl(
                conn,
                user_id=user_uuid,
                reservation_id=reservation_uuid,
                finalize_idempotency_key=req.idempotency_key,
                actuals={
                    **meta,
                    "actual_units": actual_units,
                    "external_ref_type": req.external_ref_type,
                    "external_ref_id": req.external_ref_id,
                    "service_name": caller.service_name,
                },
                channel=channel,
                country_code=country_final,
                billing_mode=billing_mode,
                billing_account_id=_as_uuid_or_none(billing_account_id),
                settlement_mode=settlement_mode,
                service_name=service_name_final or None,
                service_action=service_action_final or None,
                sku_code=sku_code_final or None,
                studio_job_id=_studio_job_uuid_or_none(req.external_ref_type, req.external_ref_id),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        row2 = await _fetch_reservation_row(conn, user_uuid, reservation_uuid)
        qb = _as_dict_loose(row2["quote_json"]) if row2 else {}

        ledger_entry_id = await _lookup_ledger_event_id(
            conn,
            user_uuid,
            f"consume:{req.idempotency_key}",
        )
        if not ledger_entry_id:
            ledger_entry_id = _quote_pick(
                qb,
                ("ledger_entry_id",),
                ("economics", "ledger_entry_id"),
                ("economics_final", "ledger_entry_id"),
                ("finalize_breakdown", "ledger_entry_id"),
            ) or None

        amount_text = _first_non_empty(
            _safe_money_str(getattr(receipt, "charged_money", None)),
            _quote_pick(
                qb,
                ("amount",),
                ("final_charged_money",),
                ("charged_money",),
                ("economics_final", "charged_money"),
                ("economics_final", "amount"),
                ("economics", "charged_money"),
                ("economics", "amount"),
            ),
            _safe_money_str(row2["estimated_money"] if row2 else None),
            "0.00",
        )

        currency_text = _first_non_empty(
            row2["currency"] if row2 else None,
            _quote_pick(
                qb,
                ("currency",),
                ("economics", "currency"),
                ("economics_final", "currency"),
            ),
            "USD",
        )

        billed_units = _first_non_empty(
            actual_units,
            _quote_pick(
                qb,
                ("billed_units",),
                ("actual_units",),
                ("finalize", "actuals", "actual_units"),
            ),
        )

        await _patch_quote_json(
            conn,
            reservation_id=reservation_uuid,
            user_id=user_uuid,
            patch={
                "actual_units": actual_units,
                "billed_units": billed_units,
                "amount": amount_text,
                "currency": currency_text,
                "ledger_entry_id": ledger_entry_id,
                "commit_idempotency_key": req.idempotency_key,
                "commit_service_name": caller.service_name,
                "external_ref_type": req.external_ref_type,
                "external_ref_id": req.external_ref_id,
                "internal_commit_state": "committed",
                "internal_commit_at": datetime.now(timezone.utc).isoformat(),
                "billing_mode_snapshot": billing_mode,
                "entitlement_source": entitlement_source,
                "billing_account_id": billing_account_id,
                "settlement_mode": settlement_mode,
                "service_name": service_name_final,
                "service_action": service_action_final,
                "sku_code": sku_code_final,
            },
        )

    return PricingCommitResponse(
        status="committed",
        reservation_id=str(reservation_uuid),
        billed_units=billed_units or None,
        amount=amount_text or None,
        currency=currency_text or None,
        ledger_entry_id=ledger_entry_id,
        billing_mode=billing_mode or None,
        entitlement_source=entitlement_source or None,
        billing_account_id=billing_account_id or None,
        settlement_mode=settlement_mode or None,
        message="Reservation committed",
    )


# ---------------------------------------------------------------------
# release -> real release_impl
# ---------------------------------------------------------------------
@router.post("/api/pricing/reservations/release", response_model=PricingReleaseResponse)
async def release_reservation(
    req: PricingReleaseRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> PricingReleaseResponse:
    caller = _require_internal_pricing_caller(authorization, x_user_id, x_service_name)

    if str(req.user_id) != str(caller.user_id):
        raise HTTPException(status_code=403, detail="user_mismatch")

    user_uuid = UUID(str(req.user_id))
    reservation_uuid = UUID(str(req.reservation_id))
    meta = _as_dict_loose(req.meta)
    channel = _first_non_empty(meta.get("channel"), "service")
    country_code = _norm_country(meta.get("country_code"))

    async with pool.acquire() as conn:
        row_before = await _fetch_reservation_row(conn, user_uuid, reservation_uuid)
        qb_before = _as_dict_loose(row_before["quote_json"]) if row_before else {}
        try:
            rv = await release_impl(
                conn,
                user_id=user_uuid,
                reservation_id=reservation_uuid,
                idempotency_key=req.idempotency_key,
                channel=channel,
                country_code=country_code,
                reason=req.reason,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await _patch_quote_json(
            conn,
            reservation_id=reservation_uuid,
            user_id=user_uuid,
            patch={
                "release_reason": req.reason,
                "release_idempotency_key": req.idempotency_key,
                "release_service_name": caller.service_name,
                "external_ref_type": req.external_ref_type,
                "external_ref_id": req.external_ref_id,
                "internal_release_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    return PricingReleaseResponse(
        status="released",
        reservation_id=str(rv.reservation_id),
        released_units=_to_units_str(getattr(rv, "reserved_credits", None), "0"),
        billing_mode=_first_non_empty(qb_before.get("billing_mode_snapshot")) or None,
        entitlement_source=_first_non_empty(qb_before.get("entitlement_source")) or None,
        billing_account_id=_first_non_empty(
            getattr(row_before, "get", lambda *_: None)("billing_account_id") if row_before else None,
            qb_before.get("billing_account_id"),
        )
        or None,
        settlement_mode=_first_non_empty(
            getattr(row_before, "get", lambda *_: None)("settlement_mode") if row_before else None,
            qb_before.get("settlement_mode"),
        )
        or None,
        message=f"Reservation released: {req.reason}",
    )


# ---------------------------------------------------------------------
# list / inspect reservations
# ---------------------------------------------------------------------
@router.get("/api/pricing/reservations/{reservation_id}", response_model=ReservationOut)
async def get_reservation(
    reservation_id: UUID,
    user_id: str = Query(...),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> ReservationOut:
    caller = _require_internal_pricing_caller(authorization, x_user_id, x_service_name)
    if str(user_id) != str(caller.user_id):
        raise HTTPException(status_code=403, detail="user_mismatch")

    user_uuid = UUID(str(user_id))
    async with pool.acquire() as conn:
        row = await _fetch_reservation_row(conn, user_uuid, reservation_id)
        if not row:
            raise HTTPException(status_code=404, detail="PRICING_RESERVATION_NOT_FOUND")
        qb = _as_dict_loose(row["quote_json"])

    expires_at = row["expires_at"]
    expires_text = expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at)

    return ReservationOut(
        reservation_id=reservation_id,
        status=str(row["status"]),
        expires_at=expires_text,
        reserved_credits=int(row["reserved_credits"] or 0),
        currency=_first_non_empty(row["currency"], "USD"),
        estimated_money=_safe_money_str(row["estimated_money"]),
        channel=_first_non_empty(row["channel"]),
        country_code=_first_non_empty(row["country_code"]),
        variant_code=_first_non_empty(qb.get("variant_code"), qb.get("sku_code")),
        category=_first_non_empty(qb.get("category")),
        billing_mode_snapshot=_first_non_empty(qb.get("billing_mode_snapshot")),
        entitlement_source=_first_non_empty(qb.get("entitlement_source")),
        hold_applied=_to_bool(qb.get("hold_applied"), default=False),
        billing_account_id=_first_non_empty(
            getattr(row, "get", lambda *_: None)("billing_account_id"),
            qb.get("billing_account_id"),
        )
        or None,
        settlement_mode=_first_non_empty(
            getattr(row, "get", lambda *_: None)("settlement_mode"),
            qb.get("settlement_mode"),
        )
        or None,
        quote_breakdown=qb,
    )