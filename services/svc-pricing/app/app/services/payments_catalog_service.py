from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

_PLAN_RANK_SQL = """
case
  when lower(coalesce(plan_code, '')) like 'enterprise%%yearly%%' then 71
  when lower(coalesce(plan_code, '')) like 'enterprise%%' then 70
  when lower(coalesce(plan_code, '')) like 'business%%yearly%%' then 61
  when lower(coalesce(plan_code, '')) like 'business%%' then 60
  when lower(coalesce(plan_code, '')) like 'pro%%yearly%%' then 51
  when lower(coalesce(plan_code, '')) like 'pro%%' then 50
  when lower(coalesce(plan_code, '')) like 'creator%%' then 40
  when lower(coalesce(plan_code, '')) = 'free' then 0
  else 10
end
"""


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


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _norm_currency(value: Optional[str]) -> str:
    return _clean_text(value).upper() or "USD"


def _currency_for_country(country_code: Optional[str]) -> str:
    return "INR" if _clean_text(country_code).upper() == "IN" else "USD"


def _symbol_for_currency(currency: str) -> str:
    ccy = _norm_currency(currency)
    if ccy == "USD":
        return "$"
    if ccy == "INR":
        return "₹"
    return f"{ccy} "


def _fmt_money_major(value: Any, currency: str, suffix: str = "") -> str:
    try:
        amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0.00")
    text = f"{amount:.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    return f"{_symbol_for_currency(currency)}{text}{suffix}"


def _default_plan_code_for_tier(tier_code: str) -> str:
    tier = _clean_text(tier_code).lower()
    if tier == "enterprise":
        return "enterprise"
    if tier == "business":
        return "business"
    if tier == "pro":
        return "pro"
    return "free"


def _default_total_credits_for_plan_code(plan_code: Optional[str]) -> Optional[int]:
    code = _clean_text(plan_code).lower()
    if code == "free":
        return 100
    if code in {"pro", "pro_monthly_v1"}:
        return 500
    if code == "pro_yearly_v1":
        return 6000
    if code in {"business", "business_monthly_v1"}:
        return 2000
    if code == "business_yearly_v1":
        return 24000
    if code.startswith("enterprise"):
        return None
    return None


def _public_plan_name(plan_code: Optional[str]) -> str:
    code = _clean_text(plan_code).lower()
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
    return code.replace("_", " ").title()


async def fetch_effective_billing_entitlement(conn: asyncpg.Connection, *, user_id: UUID) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        """
        select
          id,
          user_id,
          tier_code,
          plan_code,
          billing_mode,
          settlement_mode,
          included_credits_total,
          included_credits_remaining,
          overage_allowed,
          wallet_topup_allowed,
          hard_stop_on_insufficient_balance,
          effective_from,
          effective_to,
          source,
          metadata_json,
          created_at,
          updated_at
        from billing_entitlements
        where user_id = $1
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by effective_from desc, updated_at desc
        limit 1
        """,
        user_id,
    )

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(Decimal(str(value)))
    except Exception:
        return default

        
async def fetch_credit_account(conn: asyncpg.Connection, *, user_id: UUID) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        """
        select
          user_id,
          balance_credits,
          reserved_credits,
          billing_account_id,
          settlement_mode,
          updated_at
        from pricing_credit_accounts
        where user_id = $1
        """,
        user_id,
    )


async def fetch_pricing_account_overview(conn: asyncpg.Connection, *, user_id: UUID) -> Optional[asyncpg.Record]:
    try:
        return await conn.fetchrow(
            """
            select user_id, plan_json, lots_json, legacy_account_json
            from v_pricing_account_overview
            where user_id = $1
            """,
            user_id,
        )
    except Exception:
        return None


async def fetch_latest_subscription(conn: asyncpg.Connection, *, user_id: UUID) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        f"""
        select
          user_id,
          gateway_customer_id,
          gateway_subscription_id,
          gateway_price_id,
          plan_code,
          subscription_state,
          current_period_start,
          current_period_end,
          cancel_at_period_end,
          canceled_at,
          latest_invoice_id,
          latest_invoice_status,
          entitlement_state,
          trial_start,
          trial_end,
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
          {_PLAN_RANK_SQL} desc,
          case when cancel_at_period_end = false then 0 else 1 end,
          current_period_end desc nulls last,
          updated_at desc,
          created_at desc
        limit 1
        """,
        user_id,
    )


async def fetch_plan_catalog_rows(
    conn: asyncpg.Connection,
    *,
    currency: str,
    country_code: Optional[str],
) -> List[asyncpg.Record]:
    return await conn.fetch(
        """
        with ranked as (
          select
            t.code as tier_code,
            t.name as tier_name,
            t.monthly_grant_credits,
            tp.currency,
            tp.country_code,
            tp.monthly_price,
            tp.metadata_json,
            row_number() over (
              partition by t.code
              order by
                case when coalesce(tp.country_code, '') = coalesce($2, '') and coalesce(tp.country_code, '') <> '' then 0 else 1 end,
                case when coalesce(tp.country_code, '') = '' then 0 else 1 end,
                tp.created_at desc
            ) as rn
          from pricing_tiers t
          left join pricing_tier_prices tp
            on tp.tier_code = t.code
           and tp.is_active = true
           and upper(tp.currency) = upper($1)
           and coalesce(tp.country_code, '') in ('', coalesce($2, ''))
          where t.is_active = true
        )
        select
          tier_code,
          tier_name,
          monthly_grant_credits,
          currency,
          country_code,
          monthly_price,
          metadata_json
        from ranked
        where rn = 1
        order by coalesce((metadata_json->>'display_order')::int, 999999), tier_code asc
        """,
        currency,
        country_code or "",
    )


async def fetch_topup_pack_rows(
    conn: asyncpg.Connection,
    *,
    currency: str,
    country_code: Optional[str],
) -> List[asyncpg.Record]:
    return await conn.fetch(
        """
        with ranked as (
          select
            code,
            name,
            credits,
            currency,
            country_code,
            price_money,
            metadata_json,
            row_number() over (
              partition by code
              order by
                case when coalesce(country_code, '') = coalesce($2, '') and coalesce(country_code, '') <> '' then 0 else 1 end,
                case when coalesce(country_code, '') = '' then 0 else 1 end,
                created_at desc
            ) as rn
          from pricing_credit_packs
          where is_active = true
            and upper(currency) = upper($1)
            and coalesce(country_code, '') in ('', coalesce($2, ''))
        )
        select
          code,
          name,
          credits,
          currency,
          country_code,
          price_money,
          metadata_json
        from ranked
        where rn = 1
        order by coalesce((metadata_json->>'display_order')::int, 999999), credits asc, code asc
        """,
        currency,
        country_code or "",
    )


def build_plan_catalog_item(
    row: asyncpg.Record,
    *,
    currency: str,
    current_plan_code: Optional[str],
) -> Dict[str, Any]:
    metadata = _as_dict_loose(row["metadata_json"])
    tier_code = _clean_text(row["tier_code"]).lower()
    plan_code = _clean_text(metadata.get("plan_code")) or _default_plan_code_for_tier(tier_code)
    plan_name = _clean_text(row["tier_name"]) or tier_code.title() or "Plan"
    price_label = _clean_text(metadata.get("price_label")) or _fmt_money_major(row["monthly_price"], currency, " / month")

    is_current = _clean_text(current_plan_code).lower() == plan_code.lower() or _clean_text(current_plan_code).lower() == tier_code
    action = "current" if is_current else ("upgrade" if tier_code != "free" else "downgrade")
    cta_label = "Current plan" if is_current else (_clean_text(metadata.get("cta_label")) or (f"Choose {plan_name}" if tier_code != "free" else "Choose Free"))

    return {
        "plan_code": plan_code,
        "plan_name": plan_name,
        "price_label": price_label,
        "summary": _clean_text(metadata.get("summary")) or None,
        "feature_bullets": metadata.get("feature_bullets") if isinstance(metadata.get("feature_bullets"), list) else [],
        "limits": metadata.get("limits") if isinstance(metadata.get("limits"), dict) else {},
        "recommended": bool(metadata.get("recommended") or False),
        "contact_sales": bool(metadata.get("contact_sales") or False),
        "billing_family": _clean_text(metadata.get("billing_family")) or tier_code,
        "interval_code": _clean_text(metadata.get("interval_code")) or "monthly",
        "is_public": bool(metadata.get("is_public", True)),
        "is_active": True,
        "is_current": is_current,
        "action": "current" if is_current else action,
        "cta_label": cta_label,
        "cta_enabled": False if is_current else bool(metadata.get("cta_enabled", True)),
        "disabled_reason": None if not is_current else None,
        "display_order": int(metadata.get("display_order") or 0),
        "tier_code": tier_code,
        "monthly_grant_credits": int(row["monthly_grant_credits"] or 0),
    }


def build_topup_catalog_item(row: asyncpg.Record) -> Dict[str, Any]:
    metadata = _as_dict_loose(row["metadata_json"])
    currency = _norm_currency(row["currency"])
    return {
        "pack_code": _clean_text(row["code"]),
        "title": _clean_text(row["name"]),
        "subtitle": _clean_text(metadata.get("subtitle")) or None,
        "credits_to_grant": int(row["credits"] or 0),
        "amount_minor": int((Decimal(str(row["price_money"] or "0")) * Decimal("100")).quantize(Decimal("1"))),
        "price_label": _clean_text(metadata.get("price_label")) or _fmt_money_major(row["price_money"], currency),
        "recommended": bool(metadata.get("recommended") or False),
        "cta_label": _clean_text(metadata.get("cta_label")) or "Continue",
        "display_order": int(metadata.get("display_order") or 0),
        "is_active": True,
    }


def build_payment_overview(
    *,
    country_code: Optional[str],
    currency: str,
    current_subscription: Optional[asyncpg.Record],
    billing_entitlement: Optional[asyncpg.Record],
    credit_account: Optional[asyncpg.Record],
    current_plan_item: Optional[Dict[str, Any]],
    pricing_account_overview: Optional[asyncpg.Record] = None,
) -> Dict[str, Any]:
    current_plan_code = (
        _clean_text(billing_entitlement["plan_code"]) if billing_entitlement and billing_entitlement.get("plan_code") else ""
    ) or (
        _clean_text(current_subscription["plan_code"]) if current_subscription and current_subscription.get("plan_code") else ""
    ) or "free"

    sub_state = _clean_text(current_subscription["subscription_state"]) if current_subscription else "inactive"
    ent_state = _clean_text(current_subscription["entitlement_state"]) if current_subscription else ("active" if current_plan_code != "free" else "free")

    overview_plan = _as_dict_loose(pricing_account_overview["plan_json"]) if pricing_account_overview and pricing_account_overview.get("plan_json") is not None else {}
    overview_lots = _as_dict_loose(pricing_account_overview["lots_json"]) if pricing_account_overview and pricing_account_overview.get("lots_json") is not None else {}
    overview_legacy = _as_dict_loose(pricing_account_overview["legacy_account_json"]) if pricing_account_overview and pricing_account_overview.get("legacy_account_json") is not None else {}

    available = int(Decimal(str(overview_lots.get("total_spendable") if overview_lots.get("total_spendable") is not None else (credit_account["balance_credits"] if credit_account else 0))))
    reserved = int(Decimal(str(
        (overview_lots.get("included_reserved") or 0)
        + (overview_lots.get("promo_reserved") or 0)
        + (overview_lots.get("purchased_reserved") or 0)
        if overview_lots else (credit_account["reserved_credits"] if credit_account else 0)
    )))

    total = None
    if overview_plan:
        raw_total = overview_plan.get("included_credits_total")
        try:
            total_val = int(Decimal(str(raw_total or "0")))
            total = total_val if total_val > 0 else None
        except Exception:
            total = None
    elif billing_entitlement:
        total_val = int(Decimal(str(billing_entitlement["included_credits_total"] or "0")))
        total = total_val if total_val > 0 else None

    used = None
    if total is not None:
        remaining_included = None
        if overview_plan:
            try:
                remaining_included = int(Decimal(str(overview_plan.get("included_credits_remaining") or "0")))
            except Exception:
                remaining_included = None
        elif billing_entitlement:
            remaining_included = int(Decimal(str(billing_entitlement["included_credits_remaining"] or "0")))
        if remaining_included is not None:
            used = max(total - remaining_included, 0)
        else:
            used = max(total - available - reserved, 0)

    billing_mode = _clean_text(billing_entitlement["billing_mode"]) if billing_entitlement else (_clean_text(overview_plan.get("billing_mode")) if overview_plan else ("free" if current_plan_code == "free" else None))
    settlement_mode = _clean_text(billing_entitlement["settlement_mode"]) if billing_entitlement else (_clean_text(overview_plan.get("settlement_mode")) if overview_plan else (_clean_text(credit_account["settlement_mode"]) if credit_account else None))

    pending_change = None
    if current_subscription:
        md = _as_dict_loose(current_subscription["metadata_json"])
        pending_md = _as_dict_loose(md.get("pending_change"))
        if pending_md:
            target_plan_code = _clean_text(pending_md.get("target_plan_code")) or None
            pending_change = {
                "target_plan_code": target_plan_code,
                "target_plan_name": _public_plan_name(target_plan_code) or None,
                "effective_at": _clean_text(pending_md.get("effective_at")) or None,
                "change_mode": _clean_text(pending_md.get("change_mode")) or None,
                "status": _clean_text(pending_md.get("status")) or None,
                "target_total_credits": (
                    _safe_int(pending_md.get("target_total_credits"), 0)
                    if pending_md.get("target_total_credits") is not None
                    else _default_total_credits_for_plan_code(target_plan_code)
                ),
            }
        elif bool(current_subscription.get("cancel_at_period_end") or False):
            pending_change = {
                "target_plan_code": "free",
                "target_plan_name": "Free",
                "effective_at": current_subscription["current_period_end"].isoformat() if current_subscription.get("current_period_end") else None,
                "change_mode": "period_end",
                "status": "scheduled",
                "target_total_credits": 100,
            }

    can_manage_billing = current_plan_code != "free"
    can_cancel = can_manage_billing and not bool(current_subscription.get("cancel_at_period_end") if current_subscription else False)
    can_reactivate = bool(current_subscription.get("cancel_at_period_end") if current_subscription else False)
    can_top_up = bool(billing_entitlement["wallet_topup_allowed"]) if billing_entitlement and billing_entitlement.get("wallet_topup_allowed") is not None else True

    status_title = "Free plan" if current_plan_code == "free" else "Plan active"
    status_body = "You are on the Free plan." if current_plan_code == "free" else "Your current plan and entitlements are active."
    downgrade_notice = None
    if pending_change and _clean_text(pending_change.get("change_mode")).lower() == "period_end":
        status_title = "Change scheduled"
        effective_at = _clean_text(pending_change.get("effective_at")) or "the end of the current billing period"
        target_plan = (_clean_text((pending_change or {}).get("target_plan_name")) or _public_plan_name((pending_change or {}).get("target_plan_code"))) if pending_change else ""
        status_body = f"Your current plan stays active until {effective_at}. You can keep using your current credits until then."
        if target_plan:
            status_body += f" Scheduled next plan: {target_plan}."
        downgrade_notice = f"Scheduled change takes effect at {effective_at}."

    return {
        "country_code": _clean_text(country_code) or None,
        "currency": currency,
        "billing_mode": billing_mode or None,
        "settlement_mode": settlement_mode or None,
        "current_subscription": {
            "user_id": str(current_subscription["user_id"]) if current_subscription else None,
            "plan_code": current_plan_code,
            "subscription_state": sub_state or None,
            "entitlement_state": ent_state or None,
            "current_period_start": current_subscription["current_period_start"].isoformat() if current_subscription and current_subscription.get("current_period_start") else None,
            "current_period_end": current_subscription["current_period_end"].isoformat() if current_subscription and current_subscription.get("current_period_end") else None,
            "cancel_at_period_end": bool(current_subscription.get("cancel_at_period_end") if current_subscription else False),
            "pending_change": pending_change,
        },
        "current_plan": current_plan_item,
        "pending_change": pending_change,
        "credits": {
            "available_credits": available,
            "reserved_credits": reserved,
            "used_credits": used,
            "total_credits": total,
        },
        "header": {
            "plan_label": current_plan_item.get("plan_name") if current_plan_item else None,
            "usage_label": f"{available} available • {reserved} reserved • {used or 0} used",
            "billing_value_label": f"{available} / {total} credits available" if total is not None else f"{available} credits available",
        },
        "allowed_actions": {
            "can_manage_billing": can_manage_billing,
            "can_cancel": can_cancel,
            "can_reactivate": can_reactivate,
            "can_upgrade": True,
            "can_downgrade": current_plan_code != "free",
            "can_top_up": can_top_up,
        },
        "messages": {
            "status_title": status_title,
            "status_body": status_body,
            "downgrade_notice": downgrade_notice,
        },
    }
