from __future__ import annotations

import os
import asyncio
import hashlib
import json
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import AuthContext, AuthDep, PoolDep
from app.config import settings
from app.services.gateways.stripe_gateway import StripeGateway, StripeGatewayError
from app.services.entitlement_sync_service import clear_pending_change_metadata
from app.services.payments_catalog_service import (
    build_payment_overview,
    build_plan_catalog_item,
    build_topup_catalog_item,
    fetch_credit_account,
    fetch_pricing_account_overview,
    fetch_effective_billing_entitlement,
    fetch_latest_subscription,
    fetch_plan_catalog_rows,
    fetch_topup_pack_rows,
)

from app.services.payments_gateway_catalog import (
    enrich_plan_catalog_item_for_gateways,
    enrich_topup_catalog_item_for_gateways,
    load_apple_subscription_product_map,
    load_apple_topup_product_map,
)

from app.schemas.apple_iap import (
    AppleCreditsConfirmIn,
    AppleCreditsConfirmOut,
    AppleNotificationIn,
    AppleNotificationOut,
    AppleSubscriptionConfirmIn,
    AppleSubscriptionConfirmOut,
)
from app.schemas.google_play import (
    GoogleCreditsConfirmIn,
    GoogleCreditsConfirmOut,
    GoogleNotificationIn,
    GoogleNotificationOut,
    GoogleSubscriptionConfirmIn,
    GoogleSubscriptionConfirmOut,
)
from app.services.apple_iap_service import (
    confirm_credit_purchase as apple_confirm_credit_purchase,
    confirm_subscription_purchase as apple_confirm_subscription_purchase,
    process_notification as apple_process_notification,
)
from app.services.google_play_iap_service import (
    confirm_credit_purchase as google_confirm_credit_purchase,
    confirm_subscription_purchase as google_confirm_subscription_purchase,
    process_notification as google_process_notification,
)

router = APIRouter(prefix="/api/payments", tags=["payments"])

def _notifications_base_url() -> str:
    return str(
        os.getenv("DF_NOTIFICATIONS_URL")
        or os.getenv("DF_CORE_URL")
        or os.getenv("SVC_CORE_URL")
        or ""
    ).strip().rstrip("/")


def _notifications_internal_events_url() -> str:
    base = _notifications_base_url()
    if not base:
        return ""
    if base.endswith("/api/internal/notifications/events"):
        return base
    if base.endswith("/api"):
        return f"{base}/internal/notifications/events"
    return f"{base}/api/internal/notifications/events"


def _notifications_bearer() -> str:
    return str(
        os.getenv("DF_NOTIFICATIONS_BEARER")
        or os.getenv("SVC_TO_SVC_BEARER")
        or os.getenv("DF_PRICING_INTERNAL_BEARER")
        or ""
    ).strip()


async def _emit_notification_best_effort(payload: Dict[str, Any], *, context: Dict[str, Any]) -> None:
    url = _notifications_internal_events_url()
    token = _notifications_bearer()
    if not url or not token:
        return

    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    def _send() -> None:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    try:
        await asyncio.to_thread(_send)
    except Exception:
        pass



def _gateway() -> StripeGateway:
    gw = StripeGateway()
    if not gw.is_enabled():
        raise HTTPException(status_code=503, detail="payment_gateway_disabled")
    if settings.DF_PAYMENT_GATEWAY_PROVIDER.strip().lower() != "stripe":
        raise HTTPException(status_code=400, detail="unsupported_payment_gateway_provider")
    return gw


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


def _as_dict_deep_loose(x: Any) -> Dict[str, Any]:
    """Decode dict/JSON-string/asyncpg.Record-ish values into a plain dict.

    Some DB JSONB columns in older deployments are double encoded or contain a
    list of JSON strings. This helper is intentionally tolerant but returns only
    dictionaries, never arbitrary scalars.
    """
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (list, tuple)):
        merged: Dict[str, Any] = {}
        for item in x:
            item_dict = _as_dict_deep_loose(item)
            if item_dict:
                merged.update(item_dict)
        return merged
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            decoded = json.loads(s)
        except Exception:
            return {}
        return _as_dict_deep_loose(decoded)
    try:
        return dict(x)
    except Exception:
        return {}


def _record_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row.get(key)
        return default if value is None else value
    except Exception:
        pass
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        return default


def _json_safe(value: Any) -> Any:
    """Return a JSON-compatible copy for compatibility payload fields.

    The public overview endpoint includes a few compatibility dictionaries for
    older mobile builds. asyncpg rows, UUIDs, Decimals, and datetimes should not
    leak directly into those dicts because FastAPI/Pydantic may preserve them
    differently across versions.
    """
    if value is None:
        return None
    try:
        if not isinstance(value, (dict, list, tuple, str, int, float, bool)):
            value = dict(value)
    except Exception:
        pass
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return value


def _to_decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_int_credits(value: Any, default: int = 0) -> int:
    d = _to_decimal_or_none(value)
    if d is None:
        return default
    try:
        return max(int(d.to_integral_value(rounding="ROUND_HALF_UP")), 0)
    except Exception:
        return default


def _first_decimal(*values: Any) -> Optional[Decimal]:
    for value in values:
        d = _to_decimal_or_none(value)
        if d is not None:
            return d
    return None


def _credit_label(value: Any) -> str:
    return f"{_to_int_credits(value, 0)} credits"


def _included_label(available: int, total: int) -> str:
    return f"{max(int(available), 0)} / {max(int(total), 0)} credits"


def _cycle_usage_label(used: int, total: int) -> str:
    if total > 0:
        return f"{max(int(used), 0)} / {int(total)} credits used"
    return f"{max(int(used), 0)} credits used"


def _canonical_plan_name(plan_code: Optional[str], tier_code: Optional[str] = None) -> str:
    code = str(plan_code or "").strip()
    if code:
        return _public_plan_name(code) or _default_plan_name(code)
    tier = str(tier_code or "").strip().lower()
    if tier:
        return _default_plan_name(_plan_code_from_tier(tier))
    return "Free"


def _build_canonical_billing_display(
    *,
    overview: Dict[str, Any],
    current_ent: Any,
    credit_account: Any,
    pricing_account_overview: Any,
    current_plan_code: str,
    current_tier_code: str,
) -> Dict[str, Any]:
    """Apply the production credit display contract.

    billing_entitlements is plan metadata only. Live spendable balance always
    comes from v_pricing_account_overview lots/legacy account or the credit
    account fallback. Customer-facing display is credits-only.
    """
    ent = _as_dict_deep_loose(current_ent)
    overview_credits = _as_dict_deep_loose(overview.get("credits"))
    overview_header = _as_dict_deep_loose(overview.get("header"))
    pao = _as_dict_deep_loose(pricing_account_overview)
    plan_json = _as_dict_deep_loose(pao.get("plan_json"))
    lots_json = _as_dict_deep_loose(pao.get("lots_json"))
    legacy_account = _as_dict_deep_loose(pao.get("legacy_account_json"))
    account = _as_dict_deep_loose(credit_account)

    normalized_plan_code = str(
        current_plan_code
        or ent.get("plan_code")
        or plan_json.get("plan_code")
        or overview.get("plan_code")
        or "free"
    ).strip().lower() or "free"
    tier_code = str(
        current_tier_code
        or ent.get("tier_code")
        or plan_json.get("tier_code")
        or _tier_from_plan_code(normalized_plan_code)
    ).strip().lower() or "free"

    billing_mode = str(
        ent.get("billing_mode")
        or plan_json.get("billing_mode")
        or overview.get("billing_mode")
        or ""
    ).strip().lower()
    settlement_mode = str(
        ent.get("settlement_mode")
        or plan_json.get("settlement_mode")
        or overview.get("settlement_mode")
        or ""
    ).strip().lower()

    plan_total_decimal = _first_decimal(
        plan_json.get("included_credits_total"),
        ent.get("included_credits_total"),
        overview_credits.get("total_credits"),
    )
    plan_total = _to_int_credits(plan_total_decimal, 0)

    included_available = _to_int_credits(
        _first_decimal(
            lots_json.get("included_available"),
            lots_json.get("included_credits_available"),
        ),
        default=-1,
    )
    if included_available < 0:
        # Fallback only to live account/snapshot values. Do not use
        # billing_entitlements.included_credits_remaining here.
        included_available = _to_int_credits(
            _first_decimal(
                overview_credits.get("included_available"),
                overview_credits.get("available_credits"),
                legacy_account.get("legacy_balance_credits"),
                account.get("balance_credits"),
            ),
            0,
        )

    included_reserved = _to_int_credits(
        _first_decimal(
            lots_json.get("included_reserved"),
            lots_json.get("included_credits_reserved"),
        ),
        0,
    )
    wallet_available = _to_int_credits(
        _first_decimal(
            lots_json.get("purchased_available"),
            lots_json.get("wallet_available"),
            lots_json.get("topup_available"),
        ),
        0,
    )
    wallet_reserved = _to_int_credits(
        _first_decimal(
            lots_json.get("purchased_reserved"),
            lots_json.get("wallet_reserved"),
            lots_json.get("topup_reserved"),
        ),
        0,
    )
    promo_available = _to_int_credits(_first_decimal(lots_json.get("promo_available")), 0)
    promo_reserved = _to_int_credits(_first_decimal(lots_json.get("promo_reserved")), 0)

    total_available = _to_int_credits(
        _first_decimal(
            lots_json.get("total_spendable"),
            lots_json.get("total_available"),
            overview_credits.get("total_available"),
            overview_credits.get("available_credits"),
            legacy_account.get("legacy_balance_credits"),
            account.get("balance_credits"),
        ),
        included_available + wallet_available + promo_available,
    )
    total_reserved = _to_int_credits(
        _first_decimal(
            lots_json.get("total_reserved"),
            overview_credits.get("total_reserved"),
            overview_credits.get("reserved_credits"),
            legacy_account.get("legacy_reserved_credits"),
            account.get("reserved_credits"),
        ),
        included_reserved + wallet_reserved + promo_reserved,
    )

    # Do not infer usage from unavailable plan credits. Expired/stale included
    # credits are not customer usage. Usage must come from the canonical lots
    # read model, which computes actual committed included-credit spend.
    included_used = _to_int_credits(
        _first_decimal(lots_json.get("included_used")),
        0,
    )

    usage_percent: Optional[float] = None
    if plan_total > 0:
        usage_percent = round(min(max(included_used / plan_total, 0.0), 1.0) * 100.0, 2)

    is_enterprise = tier_code == "enterprise" or normalized_plan_code.startswith("enterprise")
    is_postpaid = settlement_mode in {"postpaid", "money", "invoice"} or billing_mode in {"postpaid", "invoice"}

    billing_model = "postpaid" if (is_enterprise or is_postpaid) else "prepaid"
    plan_name = _canonical_plan_name(normalized_plan_code, tier_code)

    if billing_model == "postpaid":
        display = {
            "header_label": f"{plan_name} • Postpaid" if plan_name else "Enterprise • Postpaid",
            "billing_label": "Monthly usage in credits",
            "included_label": "Postpaid credits",
            "wallet_label": "Not needed",
            "reserved_label": _credit_label(total_reserved),
            "cycle_usage_label": _cycle_usage_label(included_used, plan_total),
            "total_available_label": "Postpaid credits",
        }
    else:
        display = {
            "header_label": f"{total_available} available • {total_reserved} reserved",
            "billing_label": "Credits",
            "included_label": _included_label(included_available, plan_total),
            "wallet_label": _credit_label(wallet_available),
            "reserved_label": _credit_label(total_reserved),
            "cycle_usage_label": _cycle_usage_label(included_used, plan_total),
            "total_available_label": _credit_label(total_available),
        }

    canonical_plan = {
        "plan_code": normalized_plan_code,
        "plan_name": plan_name,
        "tier_code": tier_code,
        "billing_mode": billing_mode or ("invoice" if billing_model == "postpaid" else "subscription"),
        "settlement_mode": settlement_mode or ("postpaid" if billing_model == "postpaid" else "credits"),
        "included_credits_total": plan_total,
        "source": "billing_entitlements+pricing_account_overview",
    }

    canonical_credits = {
        # Backward-compatible fields consumed by existing frontend code.
        "available_credits": total_available,
        "reserved_credits": total_reserved,
        "used_credits": included_used,
        "total_credits": plan_total,

        # New explicit split used by product surfaces.
        "included_available": included_available,
        "included_reserved": included_reserved,
        "included_used": included_used,
        "wallet_available": wallet_available,
        "wallet_reserved": wallet_reserved,
        "promo_available": promo_available,
        "promo_reserved": promo_reserved,
        "total_available": total_available,
        "total_reserved": total_reserved,
        "total_spendable": total_available,
        "usage_percent": usage_percent,
        "source": "v_pricing_account_overview",
    }

    header = dict(overview_header)
    header.update(
        {
            "plan_label": plan_name,
            "plan_name": plan_name,
            "usage_label": display["header_label"],
            "billing_value_label": display["header_label"],
            "header_label": display["header_label"],
            "available_credits": total_available,
            "reserved_credits": total_reserved,
            "total_credits": plan_total,
            "billing_model": billing_model,
        }
    )

    overview["billing_model"] = billing_model
    overview["billing_mode"] = canonical_plan["billing_mode"]
    overview["settlement_mode"] = canonical_plan["settlement_mode"]
    overview["plan"] = canonical_plan
    overview["plan_code"] = canonical_plan["plan_code"]
    overview["plan_name"] = canonical_plan["plan_name"]
    overview["tier_code"] = canonical_plan["tier_code"]
    overview["credits"] = canonical_credits
    overview["header"] = header
    overview["display"] = display
    overview["billing"] = {
        "billing_model": billing_model,
        "billing_mode": canonical_plan["billing_mode"],
        "settlement_mode": canonical_plan["settlement_mode"],
        "source": "canonical_credit_display",
    }
    overview["integrity"] = {
        "source": "canonical_credit_display",
        "entitlement_remaining_legacy": ent.get("included_credits_remaining"),
        "live_total_available": total_available,
    }
    return overview


def _sha256_hex(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _minimal_payment_method_metadata(pm: Dict[str, Any], card: Dict[str, Any]) -> Dict[str, Any]:
    wallet = _as_dict_loose(card.get("wallet"))
    return {
        "provider": "stripe",
        "payment_method_id": str(pm.get("id") or "") or None,
        "type": str(pm.get("type") or "card"),
        "brand": card.get("brand"),
        "last4": card.get("last4"),
        "exp_month": card.get("exp_month"),
        "exp_year": card.get("exp_year"),
        "funding_type": card.get("funding"),
        "country_code": card.get("country"),
        "network": card.get("network"),
        "wallet_type": wallet.get("type") if isinstance(wallet, dict) else None,
    }


def _country_code_from_auth(auth: AuthContext) -> Optional[str]:
    return (
        getattr(auth, "country_code", None)
        or getattr(auth, "countryCode", None)
        or getattr(getattr(auth, "user", None), "country_code", None)
        or getattr(getattr(auth, "user", None), "countryCode", None)
        or None
    )


def _currency_for_auth(auth: AuthContext) -> str:
    return settings.currency_for_country(_country_code_from_auth(auth))


async def _load_google_subscription_product_map(
    conn,
    *,
    country_code: Optional[str],
    currency: str,
) -> Dict[str, Dict[str, str]]:
    ccy = str(currency or "USD").strip().upper() or "USD"
    cc = str(country_code or "").strip().upper()

    try:
        rows = await conn.fetch(
            """
            with ranked as (
              select
                lower(trim(internal_plan_code)) as plan_code,
                trim(google_product_id) as google_product_id,
                trim(base_plan_id) as base_plan_id,
                row_number() over (
                  partition by lower(trim(internal_plan_code))
                  order by
                    case
                      when $2 <> '' and upper(coalesce(country_code, '')) = $2 then 0
                      when coalesce(country_code, '') = '' then 1
                      else 2
                    end,
                    case
                      when upper(coalesce(currency, '')) = $1 then 0
                      when coalesce(currency, '') = '' then 1
                      else 2
                    end,
                    updated_at desc nulls last,
                    created_at desc nulls last
                ) as rn
              from public.google_play_iap_product_mappings
              where is_active = true
                and product_type = 'subscription'
                and coalesce(trim(internal_plan_code), '') <> ''
                and coalesce(trim(google_product_id), '') <> ''
                and (upper(coalesce(currency, '')) = $1 or coalesce(currency, '') = '')
                and (upper(coalesce(country_code, '')) = $2 or coalesce(country_code, '') = '')
            )
            select plan_code, google_product_id, base_plan_id
            from ranked
            where rn = 1
            """,
            ccy,
            cc,
        )
    except Exception:
        return {}

    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        plan_code = str(r["plan_code"] or "").strip().lower()
        product_id = str(r["google_product_id"] or "").strip()
        base_plan_id = str(r["base_plan_id"] or "").strip()
        if plan_code and product_id:
            out[plan_code] = {
                "google_product_id": product_id,
                "android_product_id": product_id,
                "google_base_plan_id": base_plan_id,
            }
    return out


async def _load_google_topup_product_map(
    conn,
    *,
    country_code: Optional[str],
    currency: str,
) -> Dict[str, str]:
    ccy = str(currency or "USD").strip().upper() or "USD"
    cc = str(country_code or "").strip().upper()

    try:
        rows = await conn.fetch(
            """
            with ranked as (
              select
                upper(trim(internal_pack_code)) as pack_code,
                trim(google_product_id) as google_product_id,
                row_number() over (
                  partition by upper(trim(internal_pack_code))
                  order by
                    case
                      when $2 <> '' and upper(coalesce(country_code, '')) = $2 then 0
                      when coalesce(country_code, '') = '' then 1
                      else 2
                    end,
                    case
                      when upper(coalesce(currency, '')) = $1 then 0
                      when coalesce(currency, '') = '' then 1
                      else 2
                    end,
                    updated_at desc nulls last,
                    created_at desc nulls last
                ) as rn
              from public.google_play_iap_product_mappings
              where is_active = true
                and product_type = 'consumable'
                and coalesce(trim(internal_pack_code), '') <> ''
                and coalesce(trim(google_product_id), '') <> ''
                and (upper(coalesce(currency, '')) = $1 or coalesce(currency, '') = '')
                and (upper(coalesce(country_code, '')) = $2 or coalesce(country_code, '') = '')
            )
            select pack_code, google_product_id
            from ranked
            where rn = 1
            """,
            ccy,
            cc,
        )
    except Exception:
        return {}

    return {
        str(r["pack_code"] or "").strip().upper(): str(r["google_product_id"] or "").strip()
        for r in rows
        if str(r["pack_code"] or "").strip() and str(r["google_product_id"] or "").strip()
    }


def _enrich_plan_catalog_item_for_google_play(
    item: Dict[str, Any],
    *,
    google_product_by_plan: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    out = dict(item or {})
    plan_code = str(out.get("plan_code") or "").strip().lower()
    metadata = _as_dict_deep_loose(out.get("metadata") or out.get("metadata_json"))

    google_info = google_product_by_plan.get(plan_code) or {}
    google_product_id = (
        str(out.get("google_product_id") or out.get("android_product_id") or metadata.get("google_product_id") or metadata.get("android_product_id") or "").strip()
        or google_info.get("google_product_id")
        or None
    )
    google_base_plan_id = (
        str(out.get("google_base_plan_id") or metadata.get("google_base_plan_id") or "").strip()
        or google_info.get("google_base_plan_id")
        or None
    )

    out["google_product_id"] = google_product_id
    out["android_product_id"] = google_product_id
    out["google_base_plan_id"] = google_base_plan_id

    if google_product_id:
        metadata["google_product_id"] = google_product_id
        metadata["android_product_id"] = google_product_id
    if google_base_plan_id:
        metadata["google_base_plan_id"] = google_base_plan_id

    out["metadata"] = metadata
    return out


def _enrich_topup_catalog_item_for_google_play(
    item: Dict[str, Any],
    *,
    google_product_by_pack: Dict[str, str],
) -> Dict[str, Any]:
    out = dict(item or {})
    pack_code = str(out.get("pack_code") or "").strip().upper()
    metadata = _as_dict_deep_loose(out.get("metadata") or out.get("metadata_json"))

    google_product_id = (
        str(out.get("google_product_id") or out.get("android_product_id") or metadata.get("google_product_id") or metadata.get("android_product_id") or "").strip()
        or google_product_by_pack.get(pack_code)
        or None
    )

    out["google_product_id"] = google_product_id
    out["android_product_id"] = google_product_id

    if google_product_id:
        metadata["google_product_id"] = google_product_id
        metadata["android_product_id"] = google_product_id

    out["metadata"] = metadata
    return out


def _normalize_currency(x: Optional[str]) -> str:
    return (x or settings.DF_WALLET_TOPUP_CURRENCY or "USD").strip().upper()


def _normalize_amount_minor(x: int) -> int:
    if x <= 0:
        raise HTTPException(status_code=400, detail="amount_minor_must_be_positive")
    return x


def _normalize_credits_to_grant(x: str) -> str:
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="invalid_credits_to_grant")
    if d <= 0:
        raise HTTPException(status_code=400, detail="credits_to_grant_must_be_positive")
    if d != d.to_integral_value():
        raise HTTPException(status_code=400, detail="credits_to_grant_must_be_whole_number")
    return str(int(d))


async def _sync_customer_row(conn, *, user_id: UUID, email: Optional[str], gw: StripeGateway, idempotency_key: str) -> Dict[str, Any]:
    row = await conn.fetchrow(
        "select id, gateway_customer_id, email from payment_gateway_customers where user_id = $1 and gateway_provider = 'stripe' and is_default = true limit 1",
        user_id,
    )
    if row:
        return {
            "id": str(row["id"]),
            "gateway_customer_id": str(row["gateway_customer_id"]),
            "email": row.get("email"),
        }

    customer = await gw.create_customer(
        email=email,
        metadata={"df_user_id": str(user_id), "df_service": "svc-pricing"},
        idempotency_key=idempotency_key,
    )
    rec = await conn.fetchrow(
        '''
        insert into payment_gateway_customers(
          user_id, gateway_provider, gateway_customer_id, email, is_default, created_at, updated_at
        )
        values($1, 'stripe', $2, $3, true, now(), now())
        on conflict (user_id)
        do update set
          gateway_provider = excluded.gateway_provider,
          gateway_customer_id = excluded.gateway_customer_id,
          email = coalesce(excluded.email, payment_gateway_customers.email),
          is_default = true,
          updated_at = now()
        returning id, gateway_customer_id, email
        ''',
        user_id,
        str(customer["id"]),
        email,
    )
    return {
        "id": str(rec["id"]),
        "gateway_customer_id": str(rec["gateway_customer_id"]),
        "email": rec.get("email"),
    }


async def _existing_checkout_by_idempotency(conn, *, user_id: UUID, idempotency_key: str) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        "select id, gateway_checkout_session_id, status, metadata_json, local_order_id from payment_gateway_checkout_sessions where user_id = $1 and idempotency_key = $2 limit 1",
        user_id,
        idempotency_key,
    )
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "gateway_checkout_session_id": str(row["gateway_checkout_session_id"] or ""),
        "status": str(row["status"] or ""),
        "metadata_json": _as_dict_loose(row["metadata_json"]),
        "local_order_id": str(row["local_order_id"]) if row.get("local_order_id") else None,
    }


async def _get_current_active_subscription(conn, *, user_id: UUID) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        '''
        select id, gateway_provider, plan_code, subscription_state, entitlement_state, cancel_at_period_end
        from payment_plan_subscriptions
        where user_id = $1
          and entitlement_state in ('active', 'grace')
          and subscription_state in ('trialing', 'active', 'past_due', 'unpaid', 'paused')
        order by updated_at desc
        limit 1
        ''',
        user_id,
    )
    return dict(row) if row else None


async def _get_latest_subscription_row(conn, *, user_id: UUID):
    return await conn.fetchrow(
        """
        select
          user_id,
          gateway_provider,
          gateway_customer_id,
          gateway_subscription_id,
          gateway_price_id,
          plan_code,
          subscription_state,
          current_period_start,
          current_period_end,
          cancel_at_period_end,
          latest_invoice_id,
          latest_invoice_status,
          entitlement_state,
          metadata_json,
          created_at,
          updated_at
        from payment_plan_subscriptions
        where user_id = $1
        order by
          case
            when entitlement_state in ('active', 'grace')
              or subscription_state in ('trialing', 'active', 'past_due', 'unpaid', 'paused')
            then 0 else 1
          end,
          case
            when lower(coalesce(plan_code, '')) like 'enterprise%%yearly%%' then 71
            when lower(coalesce(plan_code, '')) like 'enterprise%%' then 70
            when lower(coalesce(plan_code, '')) like 'business%%yearly%%' then 61
            when lower(coalesce(plan_code, '')) like 'business%%' then 60
            when lower(coalesce(plan_code, '')) like 'pro%%yearly%%' then 51
            when lower(coalesce(plan_code, '')) like 'pro%%' then 50
            when lower(coalesce(plan_code, '')) = 'free' then 0
            else 10
          end desc,
          case when cancel_at_period_end = false then 0 else 1 end,
          current_period_end desc nulls last,
          updated_at desc,
          created_at desc
        limit 1
        """,
        user_id,
    )



def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return default


def _plan_rank_value(plan_code: Optional[str]) -> int:
    code = str(plan_code or "").strip().lower()
    if code in {"enterprise_contract_v1", "enterprise_monthly_v1", "enterprise_yearly_v1"} or code.startswith("enterprise"):
        return 40 if "yearly" not in code else 41
    if code in {"business_monthly_v1", "business_yearly_v1", "business"} or code.startswith("business"):
        return 30 if "yearly" not in code else 31
    if code in {"pro_monthly_v1", "pro_yearly_v1", "pro"} or code.startswith("pro"):
        return 20 if "yearly" not in code else 21
    if code in {"creator"} or code.startswith("creator"):
        return 15
    return 10


def _money_label_from_major(amount: Any, currency: str, interval_code: Optional[str] = None, *, contact_sales: bool = False) -> str:
    if contact_sales:
        return "Contact sales"
    ccy = str(currency or "USD").strip().upper() or "USD"
    try:
        amt = Decimal(str(amount if amount is not None else "0"))
    except Exception:
        amt = Decimal("0")
    quant = amt.quantize(Decimal("0.01"))
    value = f"{quant:.2f}"
    if value.endswith(".00"):
        value = value[:-3]
    prefix = f"${value}" if ccy == "USD" else f"₹{value}" if ccy == "INR" else f"{ccy} {value}"
    interval = str(interval_code or "monthly").strip().lower()
    suffix = "/ year" if interval == "yearly" else "/ month" if interval == "monthly" else ""
    return f"{prefix} {suffix}".strip()


def _normalize_plan_lookup_codes(plan_code: str) -> List[str]:
    normalized = settings.normalize_plan_code(plan_code)
    candidates = [normalized]
    if normalized in {"enterprise_monthly_v1", "enterprise_yearly_v1", "enterprise"}:
        candidates = ["enterprise_contract_v1", normalized]
    elif normalized in {"business", "business_monthly"}:
        candidates = ["business_monthly_v1", normalized]
    elif normalized in {"pro", "pro_monthly"}:
        candidates = ["pro_monthly_v1", normalized]
    elif normalized in {"free"}:
        candidates = ["free"]
    out: List[str] = []
    seen = set()
    for c in candidates:
        key = str(c or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


async def _pricing_plan_prices_exists(conn) -> bool:
    try:
        row = await conn.fetchrow(
            """
            select exists (
              select 1
              from information_schema.tables
              where table_schema = 'public'
                and table_name = 'pricing_plan_prices'
            ) as present
            """
        )
        return bool(row["present"]) if row else False
    except Exception:
        return False


async def _fetch_public_recurring_plan_rows(conn, *, currency: str, country_code: Optional[str]) -> List[Dict[str, Any]]:
    ccy = str(currency or "USD").strip().upper()
    cc = str(country_code or "").strip().upper()
    if await _pricing_plan_prices_exists(conn):
        rows = await conn.fetch(
            """
            with ranked as (
              select
                p.*,
                case
                  when coalesce(p.country_code, '') = $2 then 0
                  when coalesce(p.country_code, '') = '' then 1
                  else 2
                end as pref_rank,
                row_number() over (
                  partition by lower(p.plan_code)
                  order by
                    case
                      when coalesce(p.country_code, '') = $2 then 0
                      when coalesce(p.country_code, '') = '' then 1
                      else 2
                    end,
                    p.display_order,
                    p.created_at
                ) as rn
              from public.pricing_plan_prices p
              where p.is_active = true
                and p.is_public = true
                and upper(p.currency) = $1
                and coalesce(p.country_code, '') in ($2, '')
            )
            select *
            from ranked
            where rn = 1 and pref_rank < 2
            order by display_order, lower(plan_code)
            """,
            ccy,
            cc,
        )
        return [dict(r) for r in rows]

    rows = await fetch_plan_catalog_rows(conn, currency=ccy, country_code=cc)
    return [dict(r) for r in rows]


async def _fetch_checkout_plan_row(conn, *, plan_code: str, currency: str, country_code: Optional[str]) -> Optional[Dict[str, Any]]:
    ccy = str(currency or "USD").strip().upper()
    cc = str(country_code or "").strip().upper()
    candidates = _normalize_plan_lookup_codes(plan_code)
    if await _pricing_plan_prices_exists(conn):
        row = await conn.fetchrow(
            """
            with ranked as (
              select
                p.*,
                case
                  when coalesce(p.country_code, '') = $3 then 0
                  when coalesce(p.country_code, '') = '' then 1
                  else 2
                end as pref_rank
              from public.pricing_plan_prices p
              where p.is_active = true
                and upper(p.currency) = $2
                and lower(p.plan_code) = any($1::text[])
                and coalesce(p.country_code, '') in ($3, '')
            )
            select *
            from ranked
            where pref_rank < 2
            order by pref_rank, display_order, created_at
            limit 1
            """,
            candidates,
            ccy,
            cc,
        )
        return dict(row) if row else None
    return None


def _build_plan_catalog_item_from_db_row(
    row: Dict[str, Any],
    *,
    currency: str,
    current_plan_code: Optional[str] = None,
    current_tier_code: Optional[str] = None,
) -> Dict[str, Any]:
    md = _as_dict_loose(row.get("metadata_json"))
    plan_code = str(row.get("plan_code") or md.get("plan_code") or "").strip() or "free"
    billing_family = str(row.get("tier_code") or md.get("billing_family") or _default_billing_family(plan_code)).strip().lower() or "free"
    interval_code = str(row.get("interval_code") or md.get("interval_code") or _default_interval_code(plan_code)).strip().lower() or "monthly"
    contact_sales = _coerce_bool(row.get("contact_sales"), _coerce_bool(md.get("contact_sales"), False))
    is_public = _coerce_bool(row.get("is_public"), _coerce_bool(md.get("public"), True))
    is_active = _coerce_bool(row.get("is_active"), True)
    self_serve = _coerce_bool(row.get("self_serve"), _coerce_bool(md.get("self_serve"), False))
    display_order = int(row.get("display_order") or md.get("display_order") or _plan_rank_value(plan_code))
    plan_name = str(md.get("plan_name") or row.get("name") or _default_plan_name(plan_code)).strip() or _default_plan_name(plan_code)
    summary = str(md.get("summary") or _plan_summary(plan_code, md)).strip()
    feature_bullets = _feature_bullets(md, plan_code)
    limits = _plan_limits(md, plan_code)
    price_money = row.get("price_money")
    price_label = str(md.get("price_label") or "").strip() or _money_label_from_major(
        price_money,
        currency,
        interval_code,
        contact_sales=contact_sales,
    )

    same_plan = False
    if current_plan_code:
        current_code = str(current_plan_code).strip().lower()
        same_plan = str(plan_code).strip().lower() == current_code
        if same_plan:
            interval_hint = "yearly" if "yearly" in current_code else "monthly"
            if interval_code and interval_hint and str(interval_code).strip().lower() != interval_hint:
                same_plan = False

    if same_plan:
        action = "current"
        cta_label = "Current plan"
        cta_enabled = False
    elif contact_sales:
        action = "contact_sales"
        cta_label = "Contact sales"
        cta_enabled = True
    elif not is_active:
        action = "unavailable"
        cta_label = "Unavailable"
        cta_enabled = False
    else:
        action = "change"
        cta_label = f"Choose {plan_name}"
        cta_enabled = bool(self_serve)

    disabled_reason = None
    if not is_active:
        disabled_reason = "This plan is not active in the current environment."
    elif (not contact_sales) and (not self_serve) and (not same_plan):
        disabled_reason = "This plan is not available for self-serve checkout."

    return {
        "plan_code": plan_code,
        "plan_name": plan_name,
        "price_label": price_label,
        "summary": summary,
        "feature_bullets": feature_bullets,
        "limits": limits,
        "recommended": _coerce_bool(md.get("recommended"), False),
        "contact_sales": contact_sales,
        "billing_family": billing_family,
        "interval_code": interval_code,
        "is_public": is_public,
        "is_active": is_active,
        "is_current": same_plan,
        "action": action,
        "cta_label": cta_label,
        "cta_enabled": cta_enabled,
        "disabled_reason": disabled_reason,
        "display_order": display_order,
        "stripe_price_id": str(row.get("stripe_price_id") or md.get("stripe_price_id") or "").strip() or None,
        "self_serve": self_serve,
    }


async def _resolve_plan_async(conn, plan_code: str, *, country_code: Optional[str]) -> Dict[str, Any]:
    normalized = settings.normalize_plan_code(plan_code)
    currency = settings.currency_for_country(country_code)

    row = await _fetch_checkout_plan_row(
        conn,
        plan_code=normalized,
        currency=currency,
        country_code=country_code,
    )
    if row:
        item = _build_plan_catalog_item_from_db_row(row, currency=currency)
        if item.get("contact_sales"):
            return {
                "plan_code": str(item["plan_code"]),
                "currency": currency,
                "rank": _plan_rank_value(str(item["plan_code"])),
                "contact_sales": True,
                "self_serve": False,
                "price_id": None,
            }
        price_id = str(item.get("stripe_price_id") or "").strip()
        if not price_id:
            raise HTTPException(status_code=400, detail="plan_checkout_price_not_configured")
        if not bool(item.get("self_serve")):
            raise HTTPException(status_code=400, detail="plan_not_self_serve")
        return {
            "plan_code": str(item["plan_code"]),
            "price_id": price_id,
            "currency": currency,
            "rank": _plan_rank_value(str(item["plan_code"])),
            "contact_sales": False,
            "self_serve": True,
        }

    details = settings.get_plan_details(normalized, currency=currency)
    if not details:
        raise HTTPException(status_code=400, detail="unknown_or_unconfigured_plan_code")

    price_id = str(details.get("price_id") or "").strip()
    resolved_currency = str(details.get("currency") or currency).strip().upper()
    if not price_id:
        if "enterprise" in normalized:
            return {
                "plan_code": normalized,
                "price_id": None,
                "currency": resolved_currency,
                "rank": _plan_rank_value(normalized),
                "contact_sales": True,
                "self_serve": False,
            }
        raise HTTPException(status_code=400, detail="plan_checkout_price_not_configured")

    return {
        "plan_code": normalized,
        "price_id": price_id,
        "currency": resolved_currency,
        "rank": _plan_rank_value(normalized),
        "contact_sales": False,
        "self_serve": True,
    }


async def _determine_subscription_purpose(conn, *, user_id: UUID, requested_plan_code: str) -> tuple[str, Optional[str]]:
    current = await _get_current_active_subscription(conn, user_id=user_id)
    if not current:
        return "plan_subscription", None

    current_plan_code = str(current.get("plan_code") or "").strip().lower()
    if current_plan_code == requested_plan_code and not bool(current.get("cancel_at_period_end") or False):
        raise HTTPException(status_code=409, detail="subscription_already_on_plan")

    current_rank = _plan_rank_value(current_plan_code)
    requested_rank = _plan_rank_value(requested_plan_code)
    if requested_rank > current_rank:
        return "plan_upgrade", current_plan_code
    if requested_rank < current_rank:
        return "plan_downgrade", current_plan_code
    return "plan_subscription", current_plan_code


def _default_plan_name(plan_code: str) -> str:
    code = str(plan_code or "").strip().lower()
    if code == "free":
        return "Free"
    if "enterprise" in code:
        return "Enterprise Yearly" if "yearly" in code else "Enterprise"
    if "business" in code:
        return "Business Yearly" if "yearly" in code else "Business"
    if "pro" in code:
        return "Pro Yearly" if "yearly" in code else "Pro"
    return code.replace("_", " ").title() or "Plan"


def _public_plan_name(plan_code: Optional[str]) -> str:
    code = str(plan_code or "").strip().lower()
    if not code:
        return ""
    if code == "free":
        return "Free"
    if code in {"pro_monthly_v1", "pro_monthly", "pro"}:
        return "Pro Monthly"
    if code == "pro_yearly_v1":
        return "Pro Yearly"
    if code in {"business_monthly_v1", "business_monthly", "business"}:
        return "Business Monthly"
    if code == "business_yearly_v1":
        return "Business Yearly"
    if code in {"enterprise_monthly_v1", "enterprise_monthly"}:
        return "Enterprise Monthly"
    if code in {"enterprise_yearly_v1", "enterprise_contract_v1", "enterprise"}:
        return "Enterprise"
    return _default_plan_name(code)


def _default_interval_code(plan_code: str) -> str:
    code = str(plan_code or "").strip().lower()
    if code == "free":
        return "monthly"
    if "yearly" in code:
        return "yearly"
    if "enterprise" in code:
        return "custom"
    return "monthly"


def _default_billing_family(plan_code: str) -> str:
    code = str(plan_code or "").strip().lower()
    if code.startswith("pro"):
        return "pro"
    if code.startswith("business"):
        return "business"
    if code.startswith("enterprise"):
        return "enterprise"
    return "free"


def _format_price_label(plan_code: str, details: Dict[str, Any], currency: str) -> str:
    explicit = str(details.get("price_label") or "").strip()
    if explicit:
        return explicit

    code = str(plan_code or "").strip().lower()
    if code == "free":
        return "$0 / month" if currency == "USD" else f"{currency} 0 / month"

    amount_minor = details.get("amount_minor")
    interval_code = str(details.get("interval_code") or _default_interval_code(plan_code)).strip().lower()

    if amount_minor is not None:
        try:
            amount_minor_int = int(amount_minor)
            amount = Decimal(amount_minor_int) / Decimal("100")
            if currency == "USD":
                prefix = f"${amount.quantize(Decimal('0.01')).normalize()}"
            else:
                prefix = f"{currency} {amount.quantize(Decimal('0.01')).normalize()}"
            suffix = "/ year" if "year" in interval_code else "/ month"
            return f"{prefix} {suffix}"
        except Exception:
            pass

    if code == "pro_monthly_v1":
        return "$29 / month" if currency == "USD" else f"{currency} 29 / month"
    if code == "pro_yearly_v1":
        return "$290 / year" if currency == "USD" else f"{currency} 290 / year"
    if code == "business_monthly_v1":
        return "$99 / month" if currency == "USD" else f"{currency} 99 / month"
    if code == "business_yearly_v1":
        return "$990 / year" if currency == "USD" else f"{currency} 990 / year"
    if "enterprise" in code:
        return "Contact sales"

    return ""


def _feature_bullets(details: Dict[str, Any], plan_code: str) -> List[str]:
    raw = details.get("feature_bullets") or details.get("features") or []
    if isinstance(raw, list):
        out = [str(x).strip() for x in raw if str(x).strip()]
        if out:
            return out

    code = str(plan_code or "").lower()
    if code == "free":
        return [
            "Starter access for exploration",
            "Face and Audio basics",
            "Upgrade when you need more premium usage",
        ]
    if "pro" in code:
        return [
            "Higher included monthly usage",
            "Talking Video access included",
            "Better creator throughput across Face, Audio, and Fusion",
        ]
    if "business" in code:
        return [
            "Higher scale for teams and campaigns",
            "Talking Video + Cinematic Video Direction included",
            "Broader premium access across studios",
        ]
    if "enterprise" in code:
        return [
            "Custom contract and billing model",
            "Enterprise controls and governance",
            "Custom entitlement rollout paths",
        ]
    return []


def _plan_limits(details: Dict[str, Any], plan_code: str) -> Dict[str, Any]:
    raw = details.get("limits") or {}
    if isinstance(raw, dict) and raw:
        return raw

    code = str(plan_code or "").lower()
    if code == "free":
        return {
            "face": "Starter access",
            "audio": "Starter access",
            "fusion": "Limited / gated",
            "retail": "Not included",
            "music": "Not included",
        }
    if "pro" in code:
        return {
            "face": "Expanded usage",
            "audio": "Expanded usage",
            "fusion": "Talking Video",
            "retail": "Plan dependent",
            "music": "Plan dependent",
        }
    if "business" in code:
        return {
            "face": "High capacity",
            "audio": "High capacity",
            "fusion": "Talking + Cinematic",
            "retail": "Expanded",
            "music": "Expanded",
        }
    if "enterprise" in code:
        return {
            "face": "Custom",
            "audio": "Custom",
            "fusion": "Custom",
            "retail": "Custom",
            "music": "Custom",
        }
    return {}


def _plan_summary(plan_code: str, details: Dict[str, Any]) -> str:
    summary = str(details.get("summary") or "").strip()
    if summary:
        return summary
    if "enterprise" in plan_code:
        return "Enterprise controls, governance, and custom rollout."
    if "business" in plan_code:
        return "Higher scale for teams and campaigns."
    if "pro" in plan_code:
        return "Regular creator workflows with premium storytelling access."
    return "Starter access for exploration."


def _plan_code_from_tier(tier_code: Optional[str]) -> str:
    tier = str(tier_code or "").strip().lower()
    if tier == "enterprise":
        return "enterprise_monthly_v1"
    if tier == "business":
        return "business_monthly_v1"
    if tier == "pro":
        return "pro_monthly_v1"
    return "free"


def _subscription_row_is_live(row: Optional[Dict[str, Any]]) -> bool:
    if not row:
        return False
    state = str(row.get("subscription_state") or "").strip().lower()
    entitlement_state = str(row.get("entitlement_state") or "").strip().lower()
    if state in {"trialing", "active", "past_due", "unpaid", "paused"}:
        return True
    if entitlement_state in {"active", "grace"}:
        return True
    if bool(row.get("cancel_at_period_end") or False) and state not in {"canceled", "incomplete_expired"}:
        return True
    return False


def _has_linked_subscription(row: Optional[Dict[str, Any]]) -> bool:
    if not row:
        return False
    if not bool(str(row.get("gateway_subscription_id") or "").strip()):
        return False
    return _subscription_row_is_live(row)


def _live_subscription_or_none(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return row if _subscription_row_is_live(row) else None


_PAYMENT_PROVIDER_CODES = {"stripe", "apple_iap", "google_play", "other"}
_SELF_SERVE_PAYMENT_PROVIDER_CODES = {"stripe", "apple_iap", "google_play"}


def _normalize_payment_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in _PAYMENT_PROVIDER_CODES else ""


def _subscription_provider(row: Optional[Dict[str, Any]]) -> str:
    return _normalize_payment_provider(_record_get(row, "gateway_provider", ""))


def _entitlement_provider(row: Any) -> str:
    source = _normalize_payment_provider(_record_get(row, "source", ""))
    if source:
        return source

    md = _as_dict_deep_loose(_record_get(row, "metadata_json", None))
    for key in ("provider", "gateway_provider", "source"):
        provider = _normalize_payment_provider(md.get(key))
        if provider:
            return provider
    return ""


def _canonical_subscription_provider(
    current_sub: Optional[Dict[str, Any]],
    current_ent: Any = None,
    *,
    active_payment_sources: Optional[List[str]] = None,
) -> Optional[str]:
    """Resolve the provider that owns the canonical DesiFaces entitlement.

    Provider subscription rows are payment-rail history and may legitimately
    contain more than one active source. The canonical current provider should
    follow billing_entitlements.source first when present, because that row is
    the single DesiFaces entitlement contract.
    """
    provider = _entitlement_provider(current_ent)
    if provider:
        return provider

    provider = _subscription_provider(current_sub)
    if provider:
        return provider

    sources = [
        _normalize_payment_provider(source)
        for source in (active_payment_sources or [])
    ]
    sources = [source for source in sources if source in _SELF_SERVE_PAYMENT_PROVIDER_CODES]
    if len(set(sources)) == 1:
        return sources[0]
    return None


async def _fetch_active_payment_sources(conn, *, user_id: UUID) -> List[str]:
    rows = await conn.fetch(
        """
        select distinct gateway_provider
        from public.payment_plan_subscriptions
        where user_id = $1
          and subscription_state in ('trialing', 'active', 'past_due')
          and entitlement_state in ('active', 'grace')
          and gateway_provider in ('stripe', 'apple_iap', 'google_play')
        order by gateway_provider
        """,
        user_id,
    )
    out: List[str] = []
    seen = set()
    for row in rows:
        provider = _normalize_payment_provider(_record_get(row, "gateway_provider", ""))
        if provider and provider not in seen:
            seen.add(provider)
            out.append(provider)
    return out


def _is_apple_managed_subscription(row: Optional[Dict[str, Any]]) -> bool:
    return (
        _subscription_provider(row) == "apple_iap"
        and _subscription_row_is_live(row)
    )


def _is_google_play_managed_subscription(row: Optional[Dict[str, Any]]) -> bool:
    return (
        _subscription_provider(row) == "google_play"
        and _subscription_row_is_live(row)
    )


def _is_native_iap_managed_subscription(row: Optional[Dict[str, Any]]) -> bool:
    return (
        _subscription_provider(row) in {"apple_iap", "google_play"}
        and _subscription_row_is_live(row)
    )


async def _sync_subscription_cycle_credits(conn, *, user_id: UUID) -> Dict[str, Any]:
    """Synchronize subscription included-credit lots after gateway changes.

    The DB function is the source of truth for preserving purchased/top-up
    credits, expiring old plan-included lots, and creating the current-cycle
    included lot. Route handlers call this after native IAP confirmation so
    Google Play / Apple flows cannot leave billing_entitlements updated without
    matching spendable included credits.

    Native stores can deliver the same purchase/restore more than once. The DB
    function should be idempotent, but older deployed function bodies may still
    raise the pricing_credit_lots unique constraint on duplicate source_ref.
    Use a nested transaction/savepoint so the outer payment confirmation can
    remain successful for duplicate restore/confirm callbacks.
    """
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                "select public.df_sync_subscription_cycle_credits($1::uuid) as sync_result",
                user_id,
            )
            return _as_dict_deep_loose(row["sync_result"] if row else None)
    except Exception as exc:
        message = str(exc)
        duplicate_existing_cycle = (
            "pricing_credit_lots" in message
            and "source_ref" in message
            and "already exists" in message
            and "subscription_cycle:" in message
        )
        if not duplicate_existing_cycle:
            raise

        return {
            "ok": True,
            "action": "idempotent_existing_subscription_cycle_lot_after_duplicate_key",
            "user_id": str(user_id),
            "message": "duplicate subscription-cycle credit lot already exists; treated as successful restore/confirm",
        }


def _build_subscription_view(
    *,
    current_sub,
    current_ent,
    current_plan_code: str,
    user_id: UUID,
) -> "SubscriptionCurrentOut":
    linked = _has_linked_subscription(current_sub)
    tier_code = str(current_ent.get("tier_code") or "").strip().lower() if current_ent else ""
    entitlement_state = "free"
    if linked and current_sub and str(current_sub.get("entitlement_state") or "").strip():
        entitlement_state = str(current_sub.get("entitlement_state") or "").strip()
    elif tier_code and tier_code != "free":
        entitlement_state = "active"

    if linked and current_sub:
        return SubscriptionCurrentOut(
            user_id=str(current_sub["user_id"]) if current_sub.get("user_id") else str(user_id),
            plan_code=current_plan_code or "free",
            subscription_state=str(current_sub["subscription_state"]) if current_sub.get("subscription_state") else "active",
            entitlement_state=entitlement_state,
            current_period_start=current_sub["current_period_start"].isoformat() if current_sub.get("current_period_start") else None,
            current_period_end=current_sub["current_period_end"].isoformat() if current_sub.get("current_period_end") else None,
            cancel_at_period_end=bool(current_sub.get("cancel_at_period_end") or False),
            pending_change=_pending_change_from_subscription_row(current_sub),
        )

    is_paid_entitlement = str(current_plan_code or "").strip().lower() != "free"
    return SubscriptionCurrentOut(
        user_id=str(user_id),
        plan_code=current_plan_code or "free",
        subscription_state="entitlement_active" if is_paid_entitlement else "inactive",
        entitlement_state="active" if is_paid_entitlement else "free",
        current_period_start=None,
        current_period_end=None,
        cancel_at_period_end=False,
        pending_change=None,
    )


def _normalize_overview_payload(
    *,
    overview: Dict[str, Any],
    current_sub,
    current_ent,
    current_plan_code: str,
    user_id: UUID,
    credit_account: Any = None,
    pricing_account_overview: Any = None,
    active_payment_sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    linked = _has_linked_subscription(current_sub)
    normalized_plan_code = str(current_plan_code or "").strip().lower()
    tier_code = str((current_ent or {}).get("tier_code") or "").strip().lower()
    billing_mode = str((current_ent or {}).get("billing_mode") or overview.get("billing_mode") or "").strip().lower()
    settlement_mode = str((current_ent or {}).get("settlement_mode") or overview.get("settlement_mode") or "").strip().lower()
    is_enterprise = tier_code == "enterprise" or normalized_plan_code.startswith("enterprise")
    is_postpaid_money = billing_mode == "postpaid" or settlement_mode == "money"

    subscription_view = _build_subscription_view(
        current_sub=current_sub,
        current_ent=current_ent,
        current_plan_code=current_plan_code,
        user_id=user_id,
    )
    overview["current_subscription"] = subscription_view.model_dump()

    active_payment_sources = [
        source
        for source in (active_payment_sources or [])
        if _normalize_payment_provider(source) in _SELF_SERVE_PAYMENT_PROVIDER_CODES
    ]
    provider = _canonical_subscription_provider(
        current_sub,
        current_ent,
        active_payment_sources=active_payment_sources,
    )

    allowed = dict(overview.get("allowed_actions") or {})
    if not linked:
        allowed["can_manage_billing"] = False
        allowed["can_cancel"] = False
        allowed["can_reactivate"] = False
        allowed["can_downgrade"] = False
    elif provider in {"apple_iap", "google_play"}:
        allowed["can_manage_billing"] = False
        allowed["can_cancel"] = False
        allowed["can_reactivate"] = False
        allowed["can_downgrade"] = False
    if is_enterprise:
        allowed["can_upgrade"] = False
    if is_enterprise or is_postpaid_money:
        allowed["can_top_up"] = False
    overview["allowed_actions"] = allowed

    # Do not build user-facing credit labels from pre-canonical overview data.
    # The final header/display fields are set by _build_canonical_billing_display()
    # after live v_pricing_account_overview balances have been applied.
    overview["header"] = dict(overview.get("header") or {})

    pending_change = dict(overview.get("pending_change") or {})
    target_plan_code = str(pending_change.get("target_plan_code") or "").strip()
    if target_plan_code:
        pending_change["target_plan_name"] = _public_plan_name(target_plan_code) or target_plan_code
        overview["pending_change"] = pending_change

    current_subscription = dict(overview.get("current_subscription") or {})
    current_subscription_pending = dict(current_subscription.get("pending_change") or {})
    current_target_plan_code = str(current_subscription_pending.get("target_plan_code") or "").strip()
    if current_target_plan_code:
        current_subscription_pending["target_plan_name"] = _public_plan_name(current_target_plan_code) or current_target_plan_code
        current_subscription["pending_change"] = current_subscription_pending
        overview["current_subscription"] = current_subscription

    messages = dict(overview.get("messages") or {})
    if pending_change:
        effective_at = str(
            pending_change.get("effective_at")
            or current_subscription.get("current_period_end")
            or ""
        ).strip()
        target_name = str(
            pending_change.get("target_plan_name")
            or _public_plan_name(target_plan_code)
            or target_plan_code
            or "Next plan"
        ).strip()
        if effective_at:
            messages["status_title"] = "Change scheduled"
            messages["status_body"] = (
                f"Your current plan stays active until {effective_at}. "
                f"You can keep using your current credits until then. "
                f"Scheduled next plan: {target_name}."
            )
            messages["downgrade_notice"] = f"Scheduled change takes effect at {effective_at}."

    if is_enterprise and not linked:
        messages["status_title"] = "Enterprise access active"
        messages["status_body"] = (
            "Your enterprise access is active and managed outside self-serve billing. "
            "Contact DesiFaces sales or support for plan changes and billing adjustments."
        )
    elif provider == "apple_iap" and normalized_plan_code != "free":
        messages["status_title"] = "Managed by Apple"
        messages["status_body"] = (
            "This subscription is managed through Apple on iOS. "
            "Stripe billing portal actions are unavailable for Apple-managed subscriptions."
        )
    elif provider == "google_play" and normalized_plan_code != "free":
        messages["status_title"] = "Managed by Google Play"
        messages["status_body"] = (
            "This subscription is managed through Google Play on Android. "
            "Stripe billing portal actions are unavailable for Google Play-managed subscriptions."
        )
    elif not linked and normalized_plan_code != "free":
        messages["status_title"] = "Entitlement active"
        messages["status_body"] = (
            "Your plan access is active, but this account is not linked to a live billing subscription. "
            "Manage billing, cancel, and downgrade actions are unavailable until the account is linked."
        )
    overview["messages"] = messages

    # Final production contract: customer-facing usage is credits-only and live
    # balances come from pricing account/lots, not billing entitlement remaining.
    overview = _build_canonical_billing_display(
        overview=overview,
        current_ent=current_ent,
        credit_account=credit_account,
        pricing_account_overview=pricing_account_overview,
        current_plan_code=current_plan_code,
        current_tier_code=tier_code,
    )

    # Backward-compatible top-level objects for older frontend builds.
    # These must be sourced from canonical backend truth, not from the login JWT.
    current_subscription_payload = dict(overview.get("current_subscription") or {})
    current_subscription_payload["gateway_provider"] = provider or None
    if current_sub:
        current_subscription_payload["gateway_subscription_id"] = str(_record_get(current_sub, "gateway_subscription_id", "") or "") or None
        current_subscription_payload["gateway_price_id"] = str(_record_get(current_sub, "gateway_price_id", "") or "") or None
    overview["subscription"] = _json_safe(current_subscription_payload) if current_subscription_payload else None
    overview["entitlement"] = _json_safe(dict(current_ent)) if current_ent else None
    billing_payload = dict(overview.get("billing") or {})
    billing_payload.update({
        "plan_code": overview.get("plan_code"),
        "plan_name": overview.get("plan_name"),
        "tier_code": overview.get("tier_code"),
        "current_subscription_provider": provider or None,
        "active_payment_sources": active_payment_sources,
    })
    overview["billing"] = _json_safe(billing_payload)
    return overview


async def _resolve_current_plan_code(conn, *, user_id: UUID) -> str:
    ent = await conn.fetchrow(
        """
        select plan_code, tier_code
        from billing_entitlements
        where user_id = $1
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by effective_from desc, updated_at desc
        limit 1
        """,
        user_id,
    )
    if ent:
        if ent.get("plan_code"):
            return settings.normalize_plan_code(str(ent["plan_code"]))
        return _plan_code_from_tier(ent.get("tier_code"))

    sub = await _get_latest_subscription_row(conn, user_id=user_id)
    sub = _live_subscription_or_none(dict(sub) if sub else None)
    if sub and sub.get("plan_code"):
        return settings.normalize_plan_code(str(sub["plan_code"]))

    return "free"


class PendingChangeOut(BaseModel):
    target_plan_code: Optional[str] = None
    target_plan_name: Optional[str] = None
    effective_at: Optional[str] = None
    change_mode: Optional[str] = None
    status: Optional[str] = None
    target_total_credits: Optional[int] = None

def _pending_change_from_subscription_row(row) -> Optional[PendingChangeOut]:
    if not row:
        return None

    md = _as_dict_loose(row.get("metadata_json"))
    pending = _as_dict_loose(md.get("pending_change"))
    if pending:
        target_plan_code = str(pending.get("target_plan_code") or "") or None
        return PendingChangeOut(
            target_plan_code=target_plan_code,
            target_plan_name=_public_plan_name(target_plan_code) or None,
            effective_at=str(pending.get("effective_at") or "") or None,
            change_mode=str(pending.get("change_mode") or "") or None,
            status=str(pending.get("status") or "") or None,
            target_total_credits=(
                int(pending.get("target_total_credits"))
                if pending.get("target_total_credits") is not None
                else None
            ),
        )

    if bool(row.get("cancel_at_period_end") or False):
        effective_at = row["current_period_end"].isoformat() if row.get("current_period_end") else None
        return PendingChangeOut(
            target_plan_code="free",
            target_plan_name="Free",
            effective_at=effective_at,
            change_mode="period_end",
            status="scheduled",
            target_total_credits=100,
        )

    return None


def _undo_pending_change_message(current_plan_code: Optional[str], pending: Optional[PendingChangeOut]) -> str:
    target = str((pending.target_plan_code if pending else None) or "").strip().lower()
    current = _public_plan_name(current_plan_code) or _default_plan_name(str(current_plan_code or ""))
    if target and target != "free":
        return f"Scheduled downgrade removed. {current} will continue."
    return "Pending cancellation removed. Your subscription will continue."


async def _undo_pending_change_internal(
    conn,
    *,
    user_id: UUID,
) -> "SubscriptionMutationOut":
    row = await _get_latest_subscription_row(conn, user_id=user_id)
    current_row = dict(row) if row else None
    current_row = _live_subscription_or_none(current_row)
    if not current_row or not current_row.get("plan_code") or not current_row.get("gateway_subscription_id"):
        return SubscriptionMutationOut(
            status="no_subscription",
            current_plan_code="free",
            message="There is no active subscription with a pending change.",
        )

    pending = _pending_change_from_subscription_row(current_row)
    if not pending:
        return SubscriptionMutationOut(
            status="active",
            current_plan_code=str(current_row.get("plan_code") or "free"),
            subscription_state=str(current_row.get("subscription_state") or "active"),
            cancel_at_period_end=bool(current_row.get("cancel_at_period_end") or False),
            message="There is no pending cancellation or downgrade to undo.",
        )

    gw = _gateway()
    sub_id = str(current_row["gateway_subscription_id"])
    if bool(current_row.get("cancel_at_period_end") or False):
        try:
            await gw.reactivate_subscription(
                subscription_id=sub_id,
                idempotency_key=f"undo-pending-change:{user_id}:{sub_id}",
            )
        except StripeGatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    await conn.execute(
        """
        update payment_plan_subscriptions
        set
          cancel_at_period_end = false,
          metadata_json = case
            when jsonb_typeof(metadata_json) = 'object' then metadata_json - 'pending_change'
            else '{}'::jsonb
          end,
          updated_at = now()
        where user_id = $1
          and gateway_subscription_id = $2
        """,
        user_id,
        sub_id,
    )
    await clear_pending_change_metadata(conn, user_id=user_id, gateway_subscription_id=sub_id)

    refreshed = await _get_latest_subscription_row(conn, user_id=user_id)
    refreshed_dict = dict(refreshed) if refreshed else current_row
    refreshed_dict = _live_subscription_or_none(refreshed_dict) or refreshed_dict

    return SubscriptionMutationOut(
        status="updated",
        current_plan_code=str(refreshed_dict.get("plan_code") or current_row.get("plan_code") or "free"),
        subscription_state=str(refreshed_dict.get("subscription_state") or current_row.get("subscription_state") or "active"),
        cancel_at_period_end=False,
        pending_change=None,
        message=_undo_pending_change_message(current_row.get("plan_code"), pending),
    )


class CustomerSyncIn(BaseModel):
    email: Optional[str] = None


class CustomerSyncOut(BaseModel):
    ok: bool = True
    provider: str
    customer_id: str
    user_id: str
    email: Optional[str] = None


@router.post("/customer/sync", response_model=CustomerSyncOut)
async def sync_customer(inp: CustomerSyncIn, auth: AuthContext = AuthDep, pool=PoolDep) -> CustomerSyncOut:
    gw = _gateway()
    async with pool.acquire() as conn:
        row = await _sync_customer_row(
            conn,
            user_id=auth.user_id,
            email=inp.email,
            gw=gw,
            idempotency_key=f"stripe-customer-sync:{auth.user_id}",
        )
    return CustomerSyncOut(
        provider="stripe",
        customer_id=row["gateway_customer_id"],
        user_id=str(auth.user_id),
        email=row.get("email"),
    )


class PaymentMethodOut(BaseModel):
    payment_method_id: str
    brand: Optional[str] = None
    last4: Optional[str] = None
    exp_month: Optional[int] = None
    exp_year: Optional[int] = None
    funding_type: Optional[str] = None
    is_default: bool = False


@router.get("/payment-methods", response_model=List[PaymentMethodOut])
async def list_payment_methods(auth: AuthContext = AuthDep, pool=PoolDep) -> List[PaymentMethodOut]:
    gw = _gateway()
    try:
        async with pool.acquire() as conn:
            customer = await _sync_customer_row(
                conn,
                user_id=auth.user_id,
                email=None,
                gw=gw,
                idempotency_key=f"stripe-customer-sync:{auth.user_id}",
            )
            payload = await gw.list_payment_methods(
                customer_id=customer["gateway_customer_id"],
                method_type="card",
            )
            data = payload.get("data") or []

            default_pm_id: Optional[str] = None
            try:
                customer_obj = await gw.retrieve_customer(customer["gateway_customer_id"])
                invoice_settings = _as_dict_loose(customer_obj.get("invoice_settings"))
                default_pm = invoice_settings.get("default_payment_method")
                if isinstance(default_pm, dict):
                    default_pm_id = str(default_pm.get("id") or "") or None
                else:
                    default_pm_id = str(default_pm or customer_obj.get("default_source") or "") or None
            except StripeGatewayError:
                default_pm_id = None
            except Exception:
                default_pm_id = None

            out: List[PaymentMethodOut] = []
            for pm in data:
                pm_dict = _as_dict_loose(pm)
                card = _as_dict_loose(pm_dict.get("card"))
                pm_id = str(pm_dict.get("id") or "").strip()
                if not pm_id:
                    continue

                is_default = pm_id == default_pm_id
                wallet = _as_dict_loose(card.get("wallet"))
                fingerprint_hash = _sha256_hex(card.get("fingerprint"))
                metadata_json = json.dumps(_minimal_payment_method_metadata(pm_dict, card), default=str)

                await conn.execute(
                    """
                    insert into payment_gateway_payment_methods(
                      user_id,
                      gateway_provider,
                      gateway_customer_id,
                      gateway_payment_method_id,
                      method_type,
                      status,
                      brand,
                      last4,
                      exp_month,
                      exp_year,
                      funding_type,
                      country_code,
                      wallet_type,
                      network,
                      is_default,
                      fingerprint_hash,
                      metadata_json,
                      created_at,
                      updated_at,
                      deleted_at
                    )
                    values(
                      $1, 'stripe', $2, $3, 'card', 'active',
                      $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb, now(), now(), null
                    )
                    on conflict (gateway_payment_method_id)
                    do update set
                      gateway_customer_id = excluded.gateway_customer_id,
                      method_type = excluded.method_type,
                      status = 'active',
                      brand = excluded.brand,
                      last4 = excluded.last4,
                      exp_month = excluded.exp_month,
                      exp_year = excluded.exp_year,
                      funding_type = excluded.funding_type,
                      country_code = excluded.country_code,
                      wallet_type = excluded.wallet_type,
                      network = excluded.network,
                      is_default = excluded.is_default,
                      fingerprint_hash = excluded.fingerprint_hash,
                      metadata_json = excluded.metadata_json,
                      deleted_at = null,
                      updated_at = now()
                    """,
                    auth.user_id,
                    customer["gateway_customer_id"],
                    pm_id,
                    card.get("brand"),
                    card.get("last4"),
                    card.get("exp_month"),
                    card.get("exp_year"),
                    card.get("funding"),
                    card.get("country"),
                    wallet.get("type") if isinstance(wallet, dict) else None,
                    card.get("network"),
                    is_default,
                    fingerprint_hash,
                    metadata_json,
                )

                out.append(
                    PaymentMethodOut(
                        payment_method_id=pm_id,
                        brand=card.get("brand"),
                        last4=card.get("last4"),
                        exp_month=card.get("exp_month"),
                        exp_year=card.get("exp_year"),
                        funding_type=card.get("funding"),
                        is_default=is_default,
                    )
                )
        return out
    except StripeGatewayError as exc:
        raise HTTPException(status_code=502, detail=str(exc))



def _normalize_topup_pack_code(value: Any) -> Optional[str]:
    raw = str(value or "").strip().upper()
    return raw or None


def _safe_int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(Decimal(str(value)).to_integral_value(rounding="ROUND_HALF_UP"))
    except Exception:
        return None


def _price_money_to_minor(value: Any) -> Optional[int]:
    amount = _to_decimal_or_none(value)
    if amount is None:
        return None
    try:
        return int((amount * Decimal("100")).to_integral_value(rounding="ROUND_HALF_UP"))
    except Exception:
        return None


async def _resolve_wallet_topup_pack(
    conn,
    *,
    pack_code: Optional[str],
    currency: str,
    country_code: Optional[str],
    amount_minor: Optional[int],
    credits_to_grant: Optional[str],
) -> Dict[str, Any]:
    normalized_pack_code = _normalize_topup_pack_code(pack_code)
    requested_credits = _safe_int_or_none(credits_to_grant)
    requested_amount_minor = int(amount_minor) if amount_minor is not None else None
    resolved_currency = _normalize_currency(currency)

    if not normalized_pack_code and (requested_amount_minor is None or requested_credits is None):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "topup_pack_required",
                "message": "Provide pack_code, or provide amount_minor and credits_to_grant that match a configured top-up pack.",
            },
        )

    rows = await fetch_topup_pack_rows(
        conn,
        currency=resolved_currency,
        country_code=country_code,
    )

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        try:
            item = build_topup_catalog_item(row)
        except Exception:
            item = {}

        row_metadata = _as_dict_deep_loose(_record_get(row, "metadata_json", {}))
        item_metadata = _as_dict_deep_loose(item.get("metadata") or item.get("metadata_json"))
        metadata = dict(row_metadata)
        metadata.update(item_metadata)

        candidate_pack_code = _normalize_topup_pack_code(
            item.get("pack_code")
            or _record_get(row, "code", None)
            or metadata.get("pack_code")
        )

        candidate_amount_minor = _safe_int_or_none(
            item.get("amount_minor")
            or _record_get(row, "amount_minor", None)
        )
        if candidate_amount_minor is None:
            candidate_amount_minor = _price_money_to_minor(
                _record_get(row, "price_money", None)
                or metadata.get("price_money")
            )

        candidate_credits = _safe_int_or_none(
            item.get("credits_to_grant")
            or item.get("credits")
            or _record_get(row, "credits", None)
            or _record_get(row, "credits_to_grant", None)
        )

        stripe_price_id = str(
            item.get("stripe_price_id")
            or metadata.get("stripe_price_id")
            or row_metadata.get("stripe_price_id")
            or ""
        ).strip() or None

        if not candidate_pack_code or candidate_amount_minor is None or candidate_credits is None:
            continue

        candidates.append(
            {
                "pack_code": candidate_pack_code,
                "currency": resolved_currency,
                "amount_minor": int(candidate_amount_minor),
                "credits_to_grant": int(candidate_credits),
                "stripe_price_id": stripe_price_id,
                "stripe_lookup_key": str(metadata.get("stripe_lookup_key") or "").strip() or None,
                "stripe_account_id": str(metadata.get("stripe_account_id") or "").strip() or None,
                "stripe_mode": str(metadata.get("stripe_mode") or "").strip() or None,
                "metadata": metadata,
                "catalog_item": item,
            }
        )

    match: Optional[Dict[str, Any]] = None
    for candidate in candidates:
        if normalized_pack_code:
            if candidate["pack_code"] == normalized_pack_code:
                match = candidate
                break
            continue

        if (
            requested_amount_minor is not None
            and requested_credits is not None
            and candidate["amount_minor"] == requested_amount_minor
            and candidate["credits_to_grant"] == requested_credits
        ):
            match = candidate
            break

    if not match:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "topup_pack_not_configured",
                "message": "The requested top-up does not match an active configured pack for this currency/country.",
                "requested": {
                    "pack_code": normalized_pack_code,
                    "currency": resolved_currency,
                    "country_code": country_code,
                    "amount_minor": requested_amount_minor,
                    "credits_to_grant": requested_credits,
                },
                "available_packs": [
                    {
                        "pack_code": c["pack_code"],
                        "amount_minor": c["amount_minor"],
                        "credits_to_grant": c["credits_to_grant"],
                    }
                    for c in candidates
                ],
            },
        )

    mismatch_fields: Dict[str, Any] = {}
    if normalized_pack_code and requested_amount_minor is not None and requested_amount_minor != match["amount_minor"]:
        mismatch_fields["amount_minor"] = {
            "requested": requested_amount_minor,
            "expected": match["amount_minor"],
        }
    if normalized_pack_code and requested_credits is not None and requested_credits != match["credits_to_grant"]:
        mismatch_fields["credits_to_grant"] = {
            "requested": requested_credits,
            "expected": match["credits_to_grant"],
        }
    if mismatch_fields:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "topup_pack_request_mismatch",
                "message": "The requested amount or credits do not match the configured top-up pack.",
                "pack_code": match["pack_code"],
                "mismatches": mismatch_fields,
            },
        )

    if not match.get("stripe_price_id"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "topup_stripe_price_not_configured",
                "message": "This top-up pack is missing a canonical Stripe price id.",
                "pack_code": match["pack_code"],
                "currency": resolved_currency,
            },
        )

    return match


class WalletTopupCreateIn(BaseModel):
    pack_code: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    credits_to_grant: Optional[str] = None
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None
    idempotency_key: str


class WalletTopupCreateOut(BaseModel):
    ok: bool = True
    provider: str
    checkout_session_id: str
    checkout_url: str
    wallet_order_id: str
    payment_state: str


@router.post("/wallet/topups/create-checkout-session", response_model=WalletTopupCreateOut)
async def create_wallet_topup_checkout_session(inp: WalletTopupCreateIn, auth: AuthContext = AuthDep, pool=PoolDep) -> WalletTopupCreateOut:
    gw = _gateway()
    country_code = _country_code_from_auth(auth)
    requested_currency = _normalize_currency(inp.currency or _currency_for_auth(auth))
    requested_amount_minor = _normalize_amount_minor(inp.amount_minor) if inp.amount_minor is not None else None
    requested_credits_to_grant = (
        _normalize_credits_to_grant(inp.credits_to_grant)
        if inp.credits_to_grant is not None and str(inp.credits_to_grant).strip() != ""
        else None
    )

    success_url = (inp.success_url or settings.DF_PAYMENT_SUCCESS_URL_BASE or "").strip()
    cancel_url = (inp.cancel_url or settings.DF_PAYMENT_CANCEL_URL_BASE or "").strip()
    if not success_url or not cancel_url:
        raise HTTPException(status_code=400, detail="missing_success_or_cancel_url")

    async with pool.acquire() as conn:
        existing_checkout = await _existing_checkout_by_idempotency(
            conn,
            user_id=auth.user_id,
            idempotency_key=inp.idempotency_key,
        )
        if existing_checkout:
            md = existing_checkout["metadata_json"]
            session_id = str(existing_checkout["gateway_checkout_session_id"] or md.get("checkout_session_id") or "")
            checkout_url = str(md.get("checkout_url") or "")
            if session_id and checkout_url and existing_checkout["status"] in {"created", "open"}:
                return WalletTopupCreateOut(
                    provider="stripe",
                    checkout_session_id=session_id,
                    checkout_url=checkout_url,
                    wallet_order_id=existing_checkout["local_order_id"] or "",
                    payment_state="pending",
                )

        existing = await conn.fetchrow(
            "select id, gateway_checkout_session_id, payment_state, metadata_json from payment_wallet_orders where user_id = $1 and idempotency_key = $2 limit 1",
            auth.user_id,
            inp.idempotency_key,
        )
        if existing:
            md = _as_dict_loose(existing["metadata_json"])
            session_id = str(existing.get("gateway_checkout_session_id") or md.get("checkout_session_id") or "")
            checkout_url = str(md.get("checkout_url") or "")
            if session_id and checkout_url:
                return WalletTopupCreateOut(
                    provider="stripe",
                    checkout_session_id=session_id,
                    checkout_url=checkout_url,
                    wallet_order_id=str(existing["id"]),
                    payment_state=str(existing["payment_state"]),
                )

        resolved_pack = await _resolve_wallet_topup_pack(
            conn,
            pack_code=inp.pack_code,
            currency=requested_currency,
            country_code=country_code,
            amount_minor=requested_amount_minor,
            credits_to_grant=requested_credits_to_grant,
        )
        pack_code = str(resolved_pack["pack_code"])
        currency = str(resolved_pack["currency"]).upper()
        amount_minor = int(resolved_pack["amount_minor"])
        credits_to_grant = str(int(resolved_pack["credits_to_grant"]))
        stripe_price_id = str(resolved_pack["stripe_price_id"])
        stripe_lookup_key = str(resolved_pack.get("stripe_lookup_key") or "") or None
        stripe_account_id = str(resolved_pack.get("stripe_account_id") or "") or None
        stripe_mode = str(resolved_pack.get("stripe_mode") or "") or None

        order_metadata = {
            "pack_code": pack_code,
            "stripe_price_id": stripe_price_id,
            "stripe_lookup_key": stripe_lookup_key,
            "stripe_account_id": stripe_account_id,
            "stripe_mode": stripe_mode,
            "requested": {
                "pack_code": inp.pack_code,
                "amount_minor": requested_amount_minor,
                "currency": requested_currency,
                "credits_to_grant": requested_credits_to_grant,
            },
            "source": "canonical_topup_pack",
        }

        customer = await _sync_customer_row(
            conn,
            user_id=auth.user_id,
            email=None,
            gw=gw,
            idempotency_key=f"stripe-customer-sync:{auth.user_id}",
        )
        wallet_order = await conn.fetchrow(
            """
            insert into payment_wallet_orders(
              user_id, order_type, currency, amount_minor, credits_to_grant,
              gateway_provider, payment_state, fulfillment_state, idempotency_key,
              metadata_json, created_at, updated_at
            )
            values($1, 'topup', $2, $3, $4, 'stripe', 'pending', 'pending', $5, $6::jsonb, now(), now())
            returning id
            """,
            auth.user_id,
            currency,
            amount_minor,
            Decimal(credits_to_grant),
            inp.idempotency_key,
            json.dumps(order_metadata, default=str),
        )
        wallet_order_id = str(wallet_order["id"])

        try:
            session = await gw.create_wallet_topup_checkout_session(
                customer_id=customer["gateway_customer_id"],
                amount_minor=amount_minor,
                currency=currency,
                success_url=success_url,
                cancel_url=cancel_url,
                wallet_order_id=wallet_order_id,
                user_id=str(auth.user_id),
                credits_to_grant=credits_to_grant,
                idempotency_key=inp.idempotency_key,
                price_id=stripe_price_id,
                pack_code=pack_code,
            )
        except StripeGatewayError as exc:
            await conn.execute(
                "update payment_wallet_orders set payment_state = 'failed', updated_at = now(), metadata_json = jsonb_set(coalesce(metadata_json, '{}'::jsonb), '{error}', to_jsonb($2::text), true) where id = $1",
                wallet_order_id,
                str(exc),
            )
            raise HTTPException(status_code=502, detail=str(exc))

        session_id = str(session["id"])
        checkout_url = str(session.get("url") or "")
        status = "open" if checkout_url else "created"
        session_amount_minor = int(session.get("amount_total") or amount_minor)
        metadata_json = json.dumps(
            {
                "checkout_session_id": session_id,
                "checkout_url": checkout_url,
                "stripe": session,
                "pack_code": pack_code,
                "stripe_price_id": stripe_price_id,
                "stripe_lookup_key": stripe_lookup_key,
                "stripe_account_id": stripe_account_id,
                "stripe_mode": stripe_mode,
                "credits_to_grant": credits_to_grant,
                "currency": currency,
                "amount_minor": amount_minor,
                "source": "canonical_topup_pack",
            },
            default=str,
        )

        await conn.execute(
            """
            insert into payment_gateway_checkout_sessions(
              user_id, gateway_provider, gateway_checkout_session_id, gateway_customer_id,
              mode, purpose, local_order_id, currency, amount_minor, status,
              success_url, cancel_url, expires_at, idempotency_key, metadata_json, created_at, updated_at
            )
            values($1, 'stripe', $2, $3, 'payment', 'wallet_topup', $4, $5, $6, $7, $8, $9, to_timestamp($10), $11, $12::jsonb, now(), now())
            on conflict (gateway_checkout_session_id)
            do update set
              gateway_customer_id = excluded.gateway_customer_id,
              local_order_id = excluded.local_order_id,
              currency = excluded.currency,
              amount_minor = excluded.amount_minor,
              status = excluded.status,
              success_url = excluded.success_url,
              cancel_url = excluded.cancel_url,
              expires_at = excluded.expires_at,
              idempotency_key = excluded.idempotency_key,
              metadata_json = excluded.metadata_json,
              updated_at = now()
            """,
            auth.user_id,
            session_id,
            customer["gateway_customer_id"],
            wallet_order_id,
            currency,
            session_amount_minor,
            status,
            success_url,
            cancel_url,
            int(session.get("expires_at") or 0) if session.get("expires_at") else None,
            inp.idempotency_key,
            metadata_json,
        )
        await conn.execute(
            "update payment_wallet_orders set gateway_checkout_session_id = $2, payment_state = 'pending', metadata_json = $3::jsonb, updated_at = now() where id = $1",
            wallet_order_id,
            session_id,
            metadata_json,
        )

    await _emit_notification_best_effort(
        {
            "event_type": "BILLING_TOPUP_CHECKOUT_CREATED",
            "category": "billing",
            "priority": "info",
            "source_service": "svc-pricing",
            "source_ref_type": "wallet_order",
            "source_ref_id": str(wallet_order_id),
            "actor_user_id": None,
            "title": "Top-up checkout is ready",
            "body": f"Complete checkout to add {credits_to_grant} credits to your DesiFaces account.",
            "action_route": "/pricing/plan-billing",
            "action_label": "Continue checkout",
            "image_url": None,
            "payload_json": {
                "wallet_order_id": str(wallet_order_id),
                "checkout_session_id": session_id,
                "checkout_url": checkout_url,
                "pack_code": pack_code,
                "stripe_price_id": stripe_price_id,
                "credits_to_grant": credits_to_grant,
                "currency": currency,
                "amount_minor": amount_minor,
            },
            "metadata_json": {
                "wallet_order_id": str(wallet_order_id),
                "checkout_session_id": session_id,
                "pack_code": pack_code,
                "stripe_price_id": stripe_price_id,
                "stripe_lookup_key": stripe_lookup_key,
                "credits_to_grant": credits_to_grant,
                "currency": currency,
                "amount_minor": amount_minor,
            },
            "dedupe_key": f"billing-topup-checkout:{session_id}",
            "recipients": [{"user_id": str(auth.user_id), "channels": {"in_app": True, "push": False, "email": False}}],
        },
        context={"user_id": str(auth.user_id), "wallet_order_id": str(wallet_order_id), "event_type": "BILLING_TOPUP_CHECKOUT_CREATED"},
    )

    return WalletTopupCreateOut(
        provider="stripe",
        checkout_session_id=session_id,
        checkout_url=checkout_url,
        wallet_order_id=wallet_order_id,
        payment_state="pending",
    )


class WalletOrderOut(BaseModel):
    wallet_order_id: str
    payment_state: str
    fulfillment_state: str
    credits_to_grant: str
    ledger_entry_id: Optional[str] = None
    gateway_checkout_session_id: Optional[str] = None


@router.get("/wallet/orders/{wallet_order_id}", response_model=WalletOrderOut)
async def get_wallet_order(wallet_order_id: UUID, auth: AuthContext = AuthDep, pool=PoolDep) -> WalletOrderOut:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select id, payment_state, fulfillment_state, credits_to_grant, ledger_entry_id, gateway_checkout_session_id from payment_wallet_orders where id = $1 and user_id = $2",
            wallet_order_id,
            auth.user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="wallet_order_not_found")
        return WalletOrderOut(
            wallet_order_id=str(row["id"]),
            payment_state=str(row["payment_state"]),
            fulfillment_state=str(row["fulfillment_state"]),
            credits_to_grant=str(row["credits_to_grant"]),
            ledger_entry_id=str(row["ledger_entry_id"]) if row.get("ledger_entry_id") else None,
            gateway_checkout_session_id=str(row["gateway_checkout_session_id"]) if row.get("gateway_checkout_session_id") else None,
        )


class SubscriptionCreateIn(BaseModel):
    plan_code: str
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None
    idempotency_key: str
    credit_reset_acknowledged: bool = False
    credit_reset_acknowledged_at: Optional[str] = None
    credit_reset_acknowledgement_text: Optional[str] = None


class SubscriptionCreateOut(BaseModel):
    ok: bool = True
    provider: str
    checkout_session_id: str
    checkout_url: str
    payment_state: str
    purpose: str
    plan_code: str
    current_plan_code: Optional[str] = None


PLAN_CHANGE_CREDIT_RESET_ACKNOWLEDGEMENT_TEXT = (
    "I understand that changing my plan may reset or overwrite unused plan-included "
    "credits from my current billing cycle. Purchased top-up credits are preserved."
)


def _requires_plan_credit_reset_acknowledgement(*, current_plan_code: Optional[str], target_plan_code: Optional[str]) -> bool:
    current = settings.normalize_plan_code(str(current_plan_code or "free"))
    target = settings.normalize_plan_code(str(target_plan_code or "free"))
    return bool(current and target and current != target)


def _assert_plan_credit_reset_acknowledged(
    *,
    acknowledged: bool,
    current_plan_code: Optional[str],
    target_plan_code: Optional[str],
) -> None:
    if not _requires_plan_credit_reset_acknowledgement(
        current_plan_code=current_plan_code,
        target_plan_code=target_plan_code,
    ):
        return
    if acknowledged:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "plan_credit_reset_ack_required",
            "message": (
                "Changing your plan can reset or overwrite unused plan-included credits "
                "from the current billing cycle. Purchased top-up credits are preserved. "
                "Please confirm this before changing plans."
            ),
            "current_plan_code": settings.normalize_plan_code(str(current_plan_code or "free")),
            "target_plan_code": settings.normalize_plan_code(str(target_plan_code or "free")),
            "acknowledgement_text": PLAN_CHANGE_CREDIT_RESET_ACKNOWLEDGEMENT_TEXT,
        },
    )


@router.post("/subscriptions/create-checkout-session", response_model=SubscriptionCreateOut)
async def create_subscription_checkout_session(inp: SubscriptionCreateIn, auth: AuthContext = AuthDep, pool=PoolDep) -> SubscriptionCreateOut:
    gw = _gateway()
    country_code = _country_code_from_auth(auth)

    success_url = (inp.success_url or settings.DF_PAYMENT_SUCCESS_URL_BASE or "").strip()
    cancel_url = (inp.cancel_url or settings.DF_PAYMENT_CANCEL_URL_BASE or "").strip()
    if not success_url or not cancel_url:
        raise HTTPException(status_code=400, detail="missing_success_or_cancel_url")

    async with pool.acquire() as conn:
        plan = await _resolve_plan_async(conn, inp.plan_code, country_code=country_code)
        plan_code = plan["plan_code"]
        price_id = plan["price_id"]
        currency = plan["currency"]

        if bool(plan.get("contact_sales")):
            raise HTTPException(status_code=400, detail="plan_contact_sales_only")

        existing_checkout = await _existing_checkout_by_idempotency(
            conn,
            user_id=auth.user_id,
            idempotency_key=inp.idempotency_key,
        )
        if existing_checkout:
            md = existing_checkout["metadata_json"]
            session_id = str(existing_checkout["gateway_checkout_session_id"] or md.get("checkout_session_id") or "")
            checkout_url = str(md.get("checkout_url") or "")
            if session_id and checkout_url and existing_checkout["status"] in {"created", "open"}:
                return SubscriptionCreateOut(
                    provider="stripe",
                    checkout_session_id=session_id,
                    checkout_url=checkout_url,
                    payment_state="pending",
                    purpose=str(md.get("purpose") or "plan_subscription"),
                    plan_code=str(md.get("plan_code") or plan_code),
                    current_plan_code=md.get("current_plan_code"),
                )

        purpose, current_plan_code = await _determine_subscription_purpose(
            conn,
            user_id=auth.user_id,
            requested_plan_code=plan_code,
        )
        _assert_plan_credit_reset_acknowledged(
            acknowledged=bool(inp.credit_reset_acknowledged),
            current_plan_code=current_plan_code,
            target_plan_code=plan_code,
        )
        customer = await _sync_customer_row(
            conn,
            user_id=auth.user_id,
            email=None,
            gw=gw,
            idempotency_key=f"stripe-customer-sync:{auth.user_id}",
        )

        try:
            session = await gw.create_subscription_checkout_session(
                customer_id=customer["gateway_customer_id"],
                price_id=price_id,
                plan_code=plan_code,
                purpose=purpose,
                currency=currency,
                success_url=success_url,
                cancel_url=cancel_url,
                user_id=str(auth.user_id),
                idempotency_key=inp.idempotency_key,
            )
        except StripeGatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        session_id = str(session["id"])
        checkout_url = str(session.get("url") or "")
        status = "open" if checkout_url else "created"
        amount_minor = int(session.get("amount_total") or 0) if session.get("amount_total") is not None else None

        metadata = {
            "stripe": session,
            "plan_code": plan_code,
            "current_plan_code": current_plan_code,
            "purpose": purpose,
            "requested_by_user_id": str(auth.user_id),
            "country_code": country_code,
            "currency": currency,
            "credit_reset_acknowledged": bool(inp.credit_reset_acknowledged),
            "credit_reset_acknowledged_at": inp.credit_reset_acknowledged_at,
            "credit_reset_acknowledgement_text": inp.credit_reset_acknowledgement_text,
        }

        await conn.execute(
            """
            insert into payment_gateway_checkout_sessions(
              user_id, gateway_provider, gateway_checkout_session_id, gateway_customer_id,
              mode, purpose, local_subscription_id, currency, amount_minor, status,
              success_url, cancel_url, expires_at, idempotency_key, metadata_json, created_at, updated_at
            )
            values($1, 'stripe', $2, $3, 'subscription', $4, null, $5, $6, $7, $8, $9, to_timestamp($10), $11, $12::jsonb, now(), now())
            on conflict (gateway_checkout_session_id)
            do update set
              gateway_customer_id = excluded.gateway_customer_id,
              purpose = excluded.purpose,
              currency = excluded.currency,
              amount_minor = excluded.amount_minor,
              status = excluded.status,
              success_url = excluded.success_url,
              cancel_url = excluded.cancel_url,
              expires_at = excluded.expires_at,
              idempotency_key = excluded.idempotency_key,
              metadata_json = excluded.metadata_json,
              updated_at = now()
            """,
            auth.user_id,
            session_id,
            customer["gateway_customer_id"],
            purpose,
            currency,
            amount_minor,
            status,
            success_url,
            cancel_url,
            int(session.get("expires_at") or 0) if session.get("expires_at") else None,
            inp.idempotency_key,
            json.dumps(metadata, default=str),
        )

    await _emit_notification_best_effort(
        {
            "event_type": "SUBSCRIPTION_CHECKOUT_CREATED",
            "category": "billing",
            "priority": "info",
            "source_service": "svc-pricing",
            "source_ref_type": "checkout_session",
            "source_ref_id": session_id,
            "actor_user_id": None,
            "title": "Subscription checkout is ready",
            "body": f"Complete checkout to continue with the {_public_plan_name(plan_code) or _default_plan_name(plan_code)} plan.",
            "action_route": "/pricing/plan-billing",
            "action_label": "Continue checkout",
            "image_url": None,
            "payload_json": {
                "checkout_session_id": session_id,
                "checkout_url": checkout_url,
                "plan_code": plan_code,
                "current_plan_code": current_plan_code,
                "purpose": purpose,
                "currency": currency,
            },
            "metadata_json": {
                "checkout_session_id": session_id,
                "plan_code": plan_code,
                "current_plan_code": current_plan_code,
                "purpose": purpose,
                "currency": currency,
            },
            "dedupe_key": f"subscription-checkout:{session_id}",
            "recipients": [{"user_id": str(auth.user_id), "channels": {"in_app": True, "push": False, "email": False}}],
        },
        context={"user_id": str(auth.user_id), "checkout_session_id": session_id, "event_type": "SUBSCRIPTION_CHECKOUT_CREATED", "plan_code": plan_code},
    )

    return SubscriptionCreateOut(
        provider="stripe",
        checkout_session_id=session_id,
        checkout_url=checkout_url,
        payment_state="pending",
        purpose=purpose,
        plan_code=plan_code,
        current_plan_code=current_plan_code,
    )


class SubscriptionCurrentOut(BaseModel):
    user_id: Optional[str] = None
    plan_code: Optional[str] = None
    subscription_state: Optional[str] = None
    entitlement_state: Optional[str] = None
    current_period_start: Optional[str] = None
    current_period_end: Optional[str] = None
    cancel_at_period_end: bool = False
    pending_change: Optional[PendingChangeOut] = None


class PlanCatalogItemOut(BaseModel):
    plan_code: str
    plan_name: str
    price_label: str
    summary: Optional[str] = None
    feature_bullets: List[str] = []
    limits: Dict[str, Any] = {}
    recommended: bool = False
    contact_sales: bool = False
    billing_family: Optional[str] = None
    interval_code: Optional[str] = None
    is_public: bool = True
    is_active: bool = True
    is_current: bool = False
    action: str
    cta_label: str
    cta_enabled: bool = True
    disabled_reason: Optional[str] = None
    display_order: int = 0

    # Gateway identifiers used by the mobile app to choose Stripe vs Apple IAP vs Google Play.
    stripe_price_id: Optional[str] = None
    apple_product_id: Optional[str] = None
    ios_product_id: Optional[str] = None
    google_product_id: Optional[str] = None
    android_product_id: Optional[str] = None
    google_base_plan_id: Optional[str] = None
    self_serve: Optional[bool] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PlansCatalogOut(BaseModel):
    currency: str
    current_plan_code: Optional[str] = None
    current_subscription_state: Optional[str] = None
    pending_change: Optional[PendingChangeOut] = None
    items: List[PlanCatalogItemOut]


class CreditsSummaryOut(BaseModel):
    # Backward-compatible fields
    available_credits: Optional[int] = None
    reserved_credits: Optional[int] = None
    used_credits: Optional[int] = None
    total_credits: Optional[int] = None

    # Canonical live-balance split. These are the only fields that should drive
    # customer-facing credit displays.
    included_available: Optional[int] = None
    included_reserved: Optional[int] = None
    included_used: Optional[int] = None
    wallet_available: Optional[int] = None
    wallet_reserved: Optional[int] = None
    promo_available: Optional[int] = None
    promo_reserved: Optional[int] = None
    total_available: Optional[int] = None
    total_reserved: Optional[int] = None
    total_spendable: Optional[int] = None
    usage_percent: Optional[float] = None
    source: Optional[str] = None


class OverviewHeaderOut(BaseModel):
    plan_label: Optional[str] = None
    usage_label: Optional[str] = None
    billing_value_label: Optional[str] = None
    header_label: Optional[str] = None
    available_credits: Optional[int] = None
    reserved_credits: Optional[int] = None
    total_credits: Optional[int] = None
    billing_model: Optional[str] = None


class OverviewPlanOut(BaseModel):
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None
    tier_code: Optional[str] = None
    billing_mode: Optional[str] = None
    settlement_mode: Optional[str] = None
    included_credits_total: Optional[int] = None
    source: Optional[str] = None


class AllowedActionsOut(BaseModel):
    can_manage_billing: bool = False
    can_cancel: bool = False
    can_reactivate: bool = False
    can_upgrade: bool = False
    can_downgrade: bool = False
    can_top_up: bool = False


class OverviewMessagesOut(BaseModel):
    status_title: Optional[str] = None
    status_body: Optional[str] = None
    downgrade_notice: Optional[str] = None


class PaymentsOverviewOut(BaseModel):
    user_id: str
    country_code: Optional[str] = None
    currency: str

    # Backward-compatible top-level plan fields. Several app surfaces and
    # diagnostics read these directly, so keep them aligned with ``plan``.
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None
    tier_code: Optional[str] = None

    billing_model: Optional[str] = None
    billing_mode: Optional[str] = None
    settlement_mode: Optional[str] = None
    plan: Optional[OverviewPlanOut] = None
    current_subscription: SubscriptionCurrentOut

    # Compatibility objects for earlier mobile builds. New code should prefer
    # ``plan``, ``current_subscription``, and ``credits``.
    subscription: Optional[Dict[str, Any]] = None
    entitlement: Optional[Dict[str, Any]] = None
    billing: Optional[Dict[str, Any]] = None
    current_plan: Optional[PlanCatalogItemOut] = None
    pending_change: Optional[PendingChangeOut] = None
    credits: CreditsSummaryOut
    header: OverviewHeaderOut
    display: Dict[str, Any] = Field(default_factory=dict)
    allowed_actions: AllowedActionsOut
    messages: OverviewMessagesOut


class TopupCatalogItemOut(BaseModel):
    pack_code: str
    title: str
    subtitle: Optional[str] = None
    credits_to_grant: int
    amount_minor: int
    price_label: str
    recommended: bool = False
    cta_label: Optional[str] = None
    display_order: int = 0
    is_active: bool = True

    # Gateway identifiers for web checkout and mobile IAP.
    stripe_price_id: Optional[str] = None
    apple_product_id: Optional[str] = None
    ios_product_id: Optional[str] = None
    google_product_id: Optional[str] = None
    android_product_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TopupsCatalogOut(BaseModel):
    user_id: str
    country_code: Optional[str] = None
    currency: str
    current_plan_code: Optional[str] = None
    items: List[TopupCatalogItemOut]


class SubscriptionChangeIn(BaseModel):
    target_plan_code: str
    change_mode: Optional[str] = "immediate"
    idempotency_key: Optional[str] = None
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None
    return_url: Optional[str] = None
    credit_reset_acknowledged: bool = False
    credit_reset_acknowledged_at: Optional[str] = None
    credit_reset_acknowledgement_text: Optional[str] = None


class SubscriptionMutationIn(BaseModel):
    return_url: Optional[str] = None


class SubscriptionMutationOut(BaseModel):
    ok: bool = True
    status: str
    current_plan_code: Optional[str] = None
    target_plan_code: Optional[str] = None
    change_mode: Optional[str] = None
    effective_at: Optional[str] = None
    subscription_state: Optional[str] = None
    cancel_at_period_end: Optional[bool] = None
    pending_change: Optional[PendingChangeOut] = None
    checkout_url: Optional[str] = None
    portal_url: Optional[str] = None
    message: Optional[str] = None


def _tier_from_plan_code(plan_code: Optional[str]) -> str:
    code = str(plan_code or '').strip().lower()
    if code.startswith('enterprise'):
        return 'enterprise'
    if code.startswith('business') or code.startswith('team'):
        return 'business'
    if code.startswith('pro'):
        return 'pro'
    return 'free'


def _coerce_overview_subscription(row, *, current_plan_code: str) -> SubscriptionCurrentOut:
    if not row:
        return SubscriptionCurrentOut(
            user_id=None,
            plan_code=current_plan_code or 'free',
            subscription_state='inactive',
            entitlement_state='free',
            cancel_at_period_end=False,
            pending_change=None,
        )
    return SubscriptionCurrentOut(
        user_id=str(row['user_id']) if row.get('user_id') else None,
        plan_code=str(row['plan_code']) if row.get('plan_code') else (current_plan_code or 'free'),
        subscription_state=str(row['subscription_state']) if row.get('subscription_state') else 'inactive',
        entitlement_state=str(row['entitlement_state']) if row.get('entitlement_state') else 'free',
        current_period_start=row['current_period_start'].isoformat() if row.get('current_period_start') else None,
        current_period_end=row['current_period_end'].isoformat() if row.get('current_period_end') else None,
        cancel_at_period_end=bool(row.get('cancel_at_period_end') or False),
        pending_change=_pending_change_from_subscription_row(row),
    )


@router.get("/plans/catalog", response_model=PlansCatalogOut)
async def get_plans_catalog(auth: AuthContext = AuthDep, pool=PoolDep) -> PlansCatalogOut:
    country_code = _country_code_from_auth(auth)
    currency = settings.currency_for_country(country_code)

    async with pool.acquire() as conn:
        raw_sub = await fetch_latest_subscription(conn, user_id=auth.user_id)
        current_sub = _live_subscription_or_none(raw_sub)
        current_ent = await fetch_effective_billing_entitlement(conn, user_id=auth.user_id)
        current_plan_code = (
            str(current_ent["plan_code"]) if current_ent and current_ent.get("plan_code") else ""
        ) or (
            str(current_sub["plan_code"]) if current_sub and current_sub.get("plan_code") else ""
        ) or "free"
        current_tier_code = (
            str(current_ent["tier_code"]) if current_ent and current_ent.get("tier_code") else ""
        ) or _tier_from_plan_code(current_plan_code)
        pending_change = _pending_change_from_subscription_row(current_sub)
        rows = await _fetch_public_recurring_plan_rows(conn, currency=currency, country_code=country_code)
        apple_product_by_plan = await load_apple_subscription_product_map(
            conn,
            country_code=country_code,
            currency=currency,
        )
        google_product_by_plan = await _load_google_subscription_product_map(
            conn,
            country_code=country_code,
            currency=currency,
        )

    items: List[PlanCatalogItemOut] = []
    for row in rows:
        if "price_money" in row or "stripe_price_id" in row or "self_serve" in row or "contact_sales" in row:
            item = _build_plan_catalog_item_from_db_row(
                row,
                currency=currency,
                current_plan_code=current_plan_code,
                current_tier_code=current_tier_code,
            )
        else:
            item = build_plan_catalog_item(
                row,
                currency=currency,
                current_plan_code=current_tier_code or current_plan_code,
            )

        item = enrich_plan_catalog_item_for_gateways(
            item,
            apple_product_by_plan=apple_product_by_plan,
        )
        item = _enrich_plan_catalog_item_for_google_play(
            item,
            google_product_by_plan=google_product_by_plan,
        )

        items.append(
            PlanCatalogItemOut(
                **{k: v for k, v in item.items() if k in PlanCatalogItemOut.model_fields}
            )
        )

    if not any(x.plan_code == "free" for x in items):
        items.insert(
            0,
            PlanCatalogItemOut(
                plan_code="free",
                plan_name="Free",
                price_label="$0 / month" if currency == "USD" else "₹0 / month" if currency == "INR" else f"{currency} 0 / month",
                summary="Starter access for exploration.",
                feature_bullets=[],
                limits={},
                recommended=False,
                contact_sales=False,
                billing_family="free",
                interval_code="monthly",
                is_public=True,
                is_active=True,
                is_current=(current_tier_code == "free" or _tier_from_plan_code(current_plan_code) == "free"),
                action="current" if (current_tier_code == "free" or _tier_from_plan_code(current_plan_code) == "free") else "change",
                cta_label="Current plan" if (current_tier_code == "free" or _tier_from_plan_code(current_plan_code) == "free") else "Choose Free",
                cta_enabled=not (current_tier_code == "free" or _tier_from_plan_code(current_plan_code) == "free"),
                disabled_reason=None,
                display_order=-1,
                stripe_price_id=None,
                apple_product_id=None,
                ios_product_id=None,
                google_product_id=None,
                android_product_id=None,
                google_base_plan_id=None,
                self_serve=True,
                metadata={},
            ),
        )

    items.sort(key=lambda x: (x.display_order, x.plan_name.lower(), x.plan_code.lower()))

    deduped: List[PlanCatalogItemOut] = []
    seen_codes = set()
    for item in items:
        code = str(item.plan_code or "").strip().lower()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        deduped.append(item)

    return PlansCatalogOut(
        currency=currency,
        current_plan_code=current_plan_code,
        current_subscription_state=str(current_sub["subscription_state"]) if current_sub and current_sub.get("subscription_state") else None,
        pending_change=pending_change,
        items=deduped,
    )


@router.get("/subscriptions/current", response_model=SubscriptionCurrentOut)
async def get_current_subscription(auth: AuthContext = AuthDep, pool=PoolDep) -> SubscriptionCurrentOut:
    async with pool.acquire() as conn:
        raw_row = await fetch_latest_subscription(conn, user_id=auth.user_id)
        row = _live_subscription_or_none(raw_row)
        ent = await fetch_effective_billing_entitlement(conn, user_id=auth.user_id)
        current_plan_code = (str(ent['plan_code']) if ent and ent.get('plan_code') else '') or (str(row['plan_code']) if row and row.get('plan_code') else '') or 'free'
        return _build_subscription_view(
            current_sub=row,
            current_ent=ent,
            current_plan_code=current_plan_code,
            user_id=auth.user_id,
        )


@router.get("/overview", response_model=PaymentsOverviewOut)
async def get_payments_overview(auth: AuthContext = AuthDep, pool=PoolDep) -> PaymentsOverviewOut:
    country_code = _country_code_from_auth(auth)
    currency = settings.currency_for_country(country_code)

    async with pool.acquire() as conn:
        raw_sub = await fetch_latest_subscription(conn, user_id=auth.user_id)
        current_sub = _live_subscription_or_none(raw_sub)
        current_ent = await fetch_effective_billing_entitlement(conn, user_id=auth.user_id)
        credit_account = await fetch_credit_account(conn, user_id=auth.user_id)
        pricing_overview_row = await fetch_pricing_account_overview(conn, user_id=auth.user_id)
        rows = await _fetch_public_recurring_plan_rows(conn, currency=currency, country_code=country_code)
        apple_product_by_plan = await load_apple_subscription_product_map(
            conn,
            country_code=country_code,
            currency=currency,
        )
        google_product_by_plan = await _load_google_subscription_product_map(
            conn,
            country_code=country_code,
            currency=currency,
        )
        active_payment_sources = await _fetch_active_payment_sources(conn, user_id=auth.user_id)

    pricing_plan_json = _as_dict_deep_loose((pricing_overview_row or {}).get("plan_json") if pricing_overview_row else None)
    current_plan_code = (
        str(current_ent["plan_code"]) if current_ent and current_ent.get("plan_code") else ""
    ) or (
        str(current_sub["plan_code"]) if current_sub and current_sub.get("plan_code") else ""
    ) or str(pricing_plan_json.get("plan_code") or "") or "free"
    current_plan_code = settings.normalize_plan_code(current_plan_code)
    current_tier_code = (
        str(current_ent["tier_code"]) if current_ent and current_ent.get("tier_code") else ""
    ) or str(pricing_plan_json.get("tier_code") or "") or _tier_from_plan_code(current_plan_code)

    current_plan_item = None
    for row in rows:
        if "price_money" in row or "stripe_price_id" in row or "self_serve" in row or "contact_sales" in row:
            candidate = _build_plan_catalog_item_from_db_row(
                row,
                currency=currency,
                current_plan_code=current_plan_code,
                current_tier_code=None,
            )
        else:
            candidate = build_plan_catalog_item(
                row,
                currency=currency,
                current_plan_code=current_plan_code,
            )
        candidate = enrich_plan_catalog_item_for_gateways(
            candidate,
            apple_product_by_plan=apple_product_by_plan,
        )
        candidate = _enrich_plan_catalog_item_for_google_play(
            candidate,
            google_product_by_plan=google_product_by_plan,
        )
        if str(candidate.get("plan_code") or "").strip().lower() == str(current_plan_code).strip().lower():
            candidate["is_current"] = True
            candidate["action"] = "current"
            candidate["cta_label"] = "Current plan"
            candidate["cta_enabled"] = False
            current_plan_item = candidate
            break

    if not current_plan_item:
        # synthesize current-plan tile from entitlement if no catalog row matches
        contact_sales = str(current_tier_code).strip().lower() == "enterprise"
        current_plan_item = {
            "plan_code": current_plan_code,
            "plan_name": _canonical_plan_name(current_plan_code, current_tier_code),
            "price_label": "Contact sales" if contact_sales else ("$0 / month" if currency == "USD" else "₹0 / month" if currency == "INR" else f"{currency} 0 / month"),
            "summary": _plan_summary(current_plan_code, {}),
            "feature_bullets": _feature_bullets({}, current_plan_code),
            "limits": _plan_limits({}, current_plan_code),
            "recommended": False,
            "contact_sales": contact_sales,
            "billing_family": _tier_from_plan_code(current_plan_code),
            "interval_code": "custom" if contact_sales else "monthly",
            "is_public": True,
            "is_active": True,
            "is_current": True,
            "action": "current",
            "cta_label": "Current plan",
            "cta_enabled": False,
            "disabled_reason": None,
            "display_order": _plan_rank_value(current_plan_code),
            "stripe_price_id": None,
            "apple_product_id": apple_product_by_plan.get(str(current_plan_code or "").strip().lower()),
            "ios_product_id": apple_product_by_plan.get(str(current_plan_code or "").strip().lower()),
            "google_product_id": (google_product_by_plan.get(str(current_plan_code or "").strip().lower()) or {}).get("google_product_id"),
            "android_product_id": (google_product_by_plan.get(str(current_plan_code or "").strip().lower()) or {}).get("android_product_id"),
            "google_base_plan_id": (google_product_by_plan.get(str(current_plan_code or "").strip().lower()) or {}).get("google_base_plan_id"),
            "self_serve": False,
            "metadata": {},
        }

    overview = build_payment_overview(
        country_code=country_code,
        currency=currency,
        current_subscription=current_sub,
        billing_entitlement=current_ent,
        credit_account=credit_account,
        current_plan_item=current_plan_item,
        pricing_account_overview=pricing_overview_row,
    )
    overview["user_id"] = str(auth.user_id)
    overview = _normalize_overview_payload(
        overview=overview,
        current_sub=current_sub,
        current_ent=current_ent,
        current_plan_code=current_plan_code,
        user_id=auth.user_id,
        credit_account=credit_account,
        pricing_account_overview=pricing_overview_row,
        active_payment_sources=active_payment_sources,
    )

    overview_current_plan_item = dict(current_plan_item or {})
    if overview_current_plan_item:
        # The overview endpoint powers customer-facing usage screens; keep it
        # credits-only even if the plan catalog itself carries Stripe/Apple price labels.
        overview_current_plan_item["price_label"] = str(
            (overview.get("display") or {}).get("billing_label") or "Credits"
        )

    return PaymentsOverviewOut(
        user_id=str(auth.user_id),
        country_code=overview.get("country_code"),
        currency=str(overview.get("currency") or currency),
        plan_code=overview.get("plan_code") or (overview.get("plan") or {}).get("plan_code"),
        plan_name=overview.get("plan_name") or (overview.get("plan") or {}).get("plan_name"),
        tier_code=overview.get("tier_code") or (overview.get("plan") or {}).get("tier_code"),
        billing_model=overview.get("billing_model"),
        billing_mode=overview.get("billing_mode"),
        settlement_mode=overview.get("settlement_mode"),
        plan=OverviewPlanOut(**(overview.get("plan") or {})) if overview.get("plan") else None,
        current_subscription=SubscriptionCurrentOut(**(overview.get("current_subscription") or {})),
        subscription=overview.get("subscription"),
        entitlement=overview.get("entitlement"),
        billing=overview.get("billing"),
        current_plan=PlanCatalogItemOut(**overview_current_plan_item) if overview_current_plan_item else None,
        pending_change=PendingChangeOut(**overview["pending_change"]) if overview.get("pending_change") else None,
        credits=CreditsSummaryOut(**(overview.get("credits") or {})),
        header=OverviewHeaderOut(**(overview.get("header") or {})),
        display=dict(overview.get("display") or {}),
        allowed_actions=AllowedActionsOut(**(overview.get("allowed_actions") or {})),
        messages=OverviewMessagesOut(**(overview.get("messages") or {})),
    )


@router.get("/topups/catalog", response_model=TopupsCatalogOut)
async def get_topups_catalog(auth: AuthContext = AuthDep, pool=PoolDep) -> TopupsCatalogOut:
    country_code = _country_code_from_auth(auth)
    currency = settings.currency_for_country(country_code)

    async with pool.acquire() as conn:
        raw_sub = await fetch_latest_subscription(conn, user_id=auth.user_id)
        current_sub = _live_subscription_or_none(raw_sub)
        current_ent = await fetch_effective_billing_entitlement(conn, user_id=auth.user_id)
        current_plan_code = (
            str(current_ent['plan_code']) if current_ent and current_ent.get('plan_code') else ''
        ) or (
            str(current_sub['plan_code']) if current_sub and current_sub.get('plan_code') else ''
        ) or 'free'
        rows = await fetch_topup_pack_rows(conn, currency=currency, country_code=country_code)
        apple_product_by_pack = await load_apple_topup_product_map(
            conn,
            country_code=country_code,
            currency=currency,
        )
        google_product_by_pack = await _load_google_topup_product_map(
            conn,
            country_code=country_code,
            currency=currency,
        )

    items: List[TopupCatalogItemOut] = []
    for row in rows:
        item = enrich_topup_catalog_item_for_gateways(
            build_topup_catalog_item(row),
            apple_product_by_pack=apple_product_by_pack,
        )
        item = _enrich_topup_catalog_item_for_google_play(
            item,
            google_product_by_pack=google_product_by_pack,
        )

        # build_topup_catalog_item() intentionally keeps the public payload small.
        # Reattach only Stripe catalog identifiers from pricing_credit_packs.metadata_json
        # so diagnostics and web checkout surfaces can verify Apple/Google/Stripe
        # mappings without exposing unrelated DB metadata.
        row_metadata = _as_dict_deep_loose(_record_get(row, "metadata_json", {}))
        metadata = _as_dict_deep_loose(item.get("metadata") or item.get("metadata_json"))

        stripe_price_id = str(
            item.get("stripe_price_id")
            or metadata.get("stripe_price_id")
            or row_metadata.get("stripe_price_id")
            or ""
        ).strip() or None

        if stripe_price_id:
            item["stripe_price_id"] = stripe_price_id
            metadata["stripe_price_id"] = stripe_price_id

        for key in ("stripe_lookup_key", "stripe_account_id", "stripe_mode", "stripe_product_id"):
            value = str(metadata.get(key) or row_metadata.get(key) or "").strip()
            if value:
                metadata[key] = value

        item["metadata"] = metadata

        items.append(
            TopupCatalogItemOut(
                **{k: v for k, v in item.items() if k in TopupCatalogItemOut.model_fields}
            )
        )
    items.sort(key=lambda x: (x.display_order, x.credits_to_grant, x.pack_code))

    return TopupsCatalogOut(
        user_id=str(auth.user_id),
        country_code=country_code,
        currency=currency,
        current_plan_code=current_plan_code,
        items=items,
    )


class BillingPortalIn(BaseModel):
    return_url: str


class BillingPortalOut(BaseModel):
    ok: bool = True
    portal_url: str


@router.post("/customer-portal/create-session", response_model=BillingPortalOut)
async def create_billing_portal_session(inp: BillingPortalIn, auth: AuthContext = AuthDep, pool=PoolDep) -> BillingPortalOut:
    gw = _gateway()
    if not settings.STRIPE_BILLING_PORTAL_ENABLED:
        raise HTTPException(status_code=400, detail="billing_portal_disabled")

    async with pool.acquire() as conn:
        latest_sub = await _get_latest_subscription_row(conn, user_id=auth.user_id)
        latest_sub_dict = dict(latest_sub) if latest_sub else None
        if _is_apple_managed_subscription(latest_sub_dict):
            raise HTTPException(status_code=400, detail="apple_iap_managed_subscription")
        if _is_google_play_managed_subscription(latest_sub_dict):
            raise HTTPException(status_code=400, detail="google_play_managed_subscription")
        customer = await _sync_customer_row(
            conn,
            user_id=auth.user_id,
            email=None,
            gw=gw,
            idempotency_key=f"stripe-customer-sync:{auth.user_id}",
        )
        try:
            portal = await gw.create_billing_portal_session(
                customer_id=customer["gateway_customer_id"],
                return_url=inp.return_url,
            )
        except StripeGatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
    return BillingPortalOut(portal_url=str(portal["url"]))




@router.post("/apple/subscriptions/confirm", response_model=AppleSubscriptionConfirmOut)
async def confirm_apple_subscription(inp: AppleSubscriptionConfirmIn, auth: AuthContext = AuthDep, pool=PoolDep) -> AppleSubscriptionConfirmOut:
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await apple_confirm_subscription_purchase(
                conn,
                user_id=auth.user_id,
                auth_country_code=_country_code_from_auth(auth),
                payload=inp,
            )
            await _sync_subscription_cycle_credits(conn, user_id=auth.user_id)
            return result


@router.post("/apple/credits/confirm", response_model=AppleCreditsConfirmOut)
async def confirm_apple_credits(inp: AppleCreditsConfirmIn, auth: AuthContext = AuthDep, pool=PoolDep) -> AppleCreditsConfirmOut:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await apple_confirm_credit_purchase(
                conn,
                user_id=auth.user_id,
                auth_country_code=_country_code_from_auth(auth),
                payload=inp,
            )


@router.post("/apple/notifications", response_model=AppleNotificationOut)
async def handle_apple_notification(inp: AppleNotificationIn, pool=PoolDep) -> AppleNotificationOut:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await apple_process_notification(
                conn,
                payload=inp,
            )


@router.post("/google/subscriptions/confirm", response_model=GoogleSubscriptionConfirmOut)
async def confirm_google_subscription(inp: GoogleSubscriptionConfirmIn, auth: AuthContext = AuthDep, pool=PoolDep) -> GoogleSubscriptionConfirmOut:
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await google_confirm_subscription_purchase(
                conn,
                user_id=auth.user_id,
                auth_country_code=_country_code_from_auth(auth),
                payload=inp,
            )
            await _sync_subscription_cycle_credits(conn, user_id=auth.user_id)
            return result


@router.post("/google/credits/confirm", response_model=GoogleCreditsConfirmOut)
async def confirm_google_credits(inp: GoogleCreditsConfirmIn, auth: AuthContext = AuthDep, pool=PoolDep) -> GoogleCreditsConfirmOut:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await google_confirm_credit_purchase(
                conn,
                user_id=auth.user_id,
                auth_country_code=_country_code_from_auth(auth),
                payload=inp,
            )


@router.post("/google/notifications", response_model=GoogleNotificationOut)
async def handle_google_notification(inp: GoogleNotificationIn, pool=PoolDep) -> GoogleNotificationOut:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await google_process_notification(
                conn,
                payload=inp,
            )


@router.post("/subscriptions/change", response_model=SubscriptionMutationOut)
async def change_subscription(inp: SubscriptionChangeIn, auth: AuthContext = AuthDep, pool=PoolDep) -> SubscriptionMutationOut:
    country_code = _country_code_from_auth(auth)

    async with pool.acquire() as conn:
        target_plan = await _resolve_plan_async(conn, inp.target_plan_code, country_code=country_code)
        target_plan_code = target_plan["plan_code"]
        current_row = await _get_latest_subscription_row(conn, user_id=auth.user_id)
        current_plan_code = await _resolve_current_plan_code(conn, user_id=auth.user_id)

    if settings.normalize_plan_code(current_plan_code) == settings.normalize_plan_code(target_plan_code):
        if current_row and bool(current_row.get("cancel_at_period_end") or False):
            return SubscriptionMutationOut(
                status="scheduled",
                current_plan_code=current_plan_code,
                target_plan_code=target_plan_code,
                change_mode="period_end",
                effective_at=current_row["current_period_end"].isoformat() if current_row.get("current_period_end") else None,
                subscription_state=str(current_row["subscription_state"]) if current_row.get("subscription_state") else None,
                cancel_at_period_end=True,
                pending_change=_pending_change_from_subscription_row(current_row),
                message="This plan is already scheduled on the current subscription timeline.",
            )
        raise HTTPException(status_code=409, detail="subscription_already_on_plan")

    if bool(target_plan.get("contact_sales")):
        return SubscriptionMutationOut(
            status="contact_sales_required",
            current_plan_code=current_plan_code,
            target_plan_code=target_plan_code,
            change_mode=inp.change_mode or "immediate",
            message="This plan is available through DesiFaces sales.",
        )

    current_rank = _plan_rank_value(current_plan_code)
    target_rank = int(target_plan.get("rank") or _plan_rank_value(target_plan_code))
    has_linked_subscription = _has_linked_subscription(current_row)

    if _is_apple_managed_subscription(current_row):
        return SubscriptionMutationOut(
            status="apple_iap_managed",
            current_plan_code=current_plan_code,
            target_plan_code=target_plan_code,
            change_mode=inp.change_mode or "immediate",
            message="This subscription is managed through Apple on iOS. Use the Apple purchase flow to change the plan.",
        )
    if _is_google_play_managed_subscription(current_row):
        return SubscriptionMutationOut(
            status="google_play_managed",
            current_plan_code=current_plan_code,
            target_plan_code=target_plan_code,
            change_mode=inp.change_mode or "immediate",
            message="This subscription is managed through Google Play on Android. Use the Google Play purchase flow to change the plan.",
        )

    if (not has_linked_subscription) and current_rank > _plan_rank_value("free") and target_rank <= current_rank:
        return SubscriptionMutationOut(
            status="manual_change_required",
            current_plan_code=current_plan_code,
            target_plan_code=target_plan_code,
            change_mode=inp.change_mode or "immediate",
            message="This account has active plan access but is not linked to a live Stripe subscription. Contact support to change or remove it.",
        )

    _assert_plan_credit_reset_acknowledged(
        acknowledged=bool(inp.credit_reset_acknowledged),
        current_plan_code=current_plan_code,
        target_plan_code=target_plan_code,
    )

    wants_checkout = target_rank > current_rank

    if wants_checkout:
        create_out = await create_subscription_checkout_session(
            SubscriptionCreateIn(
                plan_code=target_plan_code,
                success_url=inp.success_url,
                cancel_url=inp.cancel_url,
                idempotency_key=inp.idempotency_key or f"sub-change:{auth.user_id}:{target_plan_code}",
                credit_reset_acknowledged=bool(inp.credit_reset_acknowledged),
                credit_reset_acknowledged_at=inp.credit_reset_acknowledged_at,
                credit_reset_acknowledgement_text=inp.credit_reset_acknowledgement_text,
            ),
            auth=auth,
            pool=pool,
        )
        return SubscriptionMutationOut(
            status="checkout_required",
            current_plan_code=current_plan_code,
            target_plan_code=target_plan_code,
            change_mode=inp.change_mode or "immediate",
            checkout_url=create_out.checkout_url,
            message="Redirect the user to checkout to complete this subscription change.",
        )

    portal = await create_billing_portal_session(
        BillingPortalIn(return_url=(inp.return_url or inp.success_url or settings.DF_PAYMENT_SUCCESS_URL_BASE or "").strip()),
        auth=auth,
        pool=pool,
    )
    return SubscriptionMutationOut(
        status="portal_required",
        current_plan_code=current_plan_code,
        target_plan_code=target_plan_code,
        change_mode=inp.change_mode or "immediate",
        portal_url=portal.portal_url,
        message="Use the billing portal to complete this downgrade or lateral plan change.",
    )


@router.post("/subscriptions/undo-pending-change", response_model=SubscriptionMutationOut)
async def undo_pending_change(inp: SubscriptionMutationIn = SubscriptionMutationIn(), auth: AuthContext = AuthDep, pool=PoolDep) -> SubscriptionMutationOut:
    async with pool.acquire() as conn:
        return await _undo_pending_change_internal(
            conn,
            user_id=auth.user_id,
        )


@router.post("/subscriptions/cancel", response_model=SubscriptionMutationOut)
async def cancel_subscription(inp: SubscriptionMutationIn = SubscriptionMutationIn(), auth: AuthContext = AuthDep, pool=PoolDep) -> SubscriptionMutationOut:
    async with pool.acquire() as conn:
        row = await _get_latest_subscription_row(conn, user_id=auth.user_id)

    if not row or not row.get("plan_code") or settings.normalize_plan_code(str(row["plan_code"])) == "free":
        return SubscriptionMutationOut(
            status="no_subscription",
            current_plan_code="free",
            message="There is no paid subscription to cancel.",
        )

    if _is_apple_managed_subscription(dict(row) if row else None):
        return SubscriptionMutationOut(
            status="apple_iap_managed",
            current_plan_code=str(row["plan_code"]),
            target_plan_code="free",
            change_mode="period_end",
            effective_at=row["current_period_end"].isoformat() if row.get("current_period_end") else None,
            subscription_state=str(row["subscription_state"]) if row.get("subscription_state") else None,
            cancel_at_period_end=bool(row.get("cancel_at_period_end") or False),
            message="This subscription is managed through Apple on iOS. Cancel it from Apple subscription management.",
        )
    if _is_google_play_managed_subscription(dict(row) if row else None):
        return SubscriptionMutationOut(
            status="google_play_managed",
            current_plan_code=str(row["plan_code"]),
            target_plan_code="free",
            change_mode="period_end",
            effective_at=row["current_period_end"].isoformat() if row.get("current_period_end") else None,
            subscription_state=str(row["subscription_state"]) if row.get("subscription_state") else None,
            cancel_at_period_end=bool(row.get("cancel_at_period_end") or False),
            message="This subscription is managed through Google Play on Android. Cancel it from Google Play subscription management.",
        )

    if bool(row.get("cancel_at_period_end") or False):
        return SubscriptionMutationOut(
            status="scheduled",
            current_plan_code=str(row["plan_code"]),
            target_plan_code="free",
            change_mode="period_end",
            effective_at=row["current_period_end"].isoformat() if row.get("current_period_end") else None,
            subscription_state=str(row["subscription_state"]) if row.get("subscription_state") else None,
            cancel_at_period_end=True,
            pending_change=_pending_change_from_subscription_row(row),
            message="Cancellation is already scheduled for period end.",
        )

    portal = await create_billing_portal_session(
        BillingPortalIn(return_url=(inp.return_url or settings.DF_PAYMENT_SUCCESS_URL_BASE or "").strip()),
        auth=auth,
        pool=pool,
    )
    return SubscriptionMutationOut(
        status="portal_required",
        current_plan_code=str(row["plan_code"]),
        target_plan_code="free",
        change_mode="period_end",
        effective_at=row["current_period_end"].isoformat() if row.get("current_period_end") else None,
        subscription_state=str(row["subscription_state"]) if row.get("subscription_state") else None,
        cancel_at_period_end=bool(row.get("cancel_at_period_end") or False),
        portal_url=portal.portal_url,
        message="Use the billing portal to schedule cancellation at period end.",
    )


@router.post("/subscriptions/reactivate", response_model=SubscriptionMutationOut)
async def reactivate_subscription(inp: SubscriptionMutationIn = SubscriptionMutationIn(), auth: AuthContext = AuthDep, pool=PoolDep) -> SubscriptionMutationOut:
    async with pool.acquire() as conn:
        row = await _get_latest_subscription_row(conn, user_id=auth.user_id)
        if _is_apple_managed_subscription(dict(row) if row else None):
            return SubscriptionMutationOut(
                status="apple_iap_managed",
                current_plan_code=str(row["plan_code"]) if row and row.get("plan_code") else "free",
                message="This subscription is managed through Apple on iOS. Reactivate it from Apple subscription management.",
            )
        if _is_google_play_managed_subscription(dict(row) if row else None):
            return SubscriptionMutationOut(
                status="google_play_managed",
                current_plan_code=str(row["plan_code"]) if row and row.get("plan_code") else "free",
                message="This subscription is managed through Google Play on Android. Reactivate it from Google Play subscription management.",
            )
        return await _undo_pending_change_internal(
            conn,
            user_id=auth.user_id,
        )
