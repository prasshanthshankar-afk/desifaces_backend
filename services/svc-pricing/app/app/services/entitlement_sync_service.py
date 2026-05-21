from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.repo.billing_entitlements_repo import BillingEntitlementsRepo
from app.repo.entitlements_repo import EntitlementsRepo
from app.repo.payment_plan_subscriptions_repo import PaymentPlanSubscriptionsRepo
from app.services.reservations.reservation_service import _ensure_account_row, _ledger_event


_BILLING_ENTITLEMENTS_REPO = BillingEntitlementsRepo()
_ENTITLEMENTS_REPO = EntitlementsRepo()
_PAYMENT_PLAN_SUBSCRIPTIONS_REPO = PaymentPlanSubscriptionsRepo()


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _to_uuid_or_none(x: Any) -> Optional[UUID]:
    try:
        return UUID(str(x)) if x else None
    except Exception:
        return None


def _to_iso_or_none(epoch_like: Any) -> Optional[str]:
    if epoch_like in (None, ""):
        return None
    try:
        ts = int(epoch_like)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _to_datetime_or_none(epoch_like: Any) -> Optional[datetime]:
    if epoch_like in (None, ""):
        return None
    try:
        ts = int(epoch_like)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def _normalize_subscription_state(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in {"trialing", "active", "past_due", "unpaid", "paused", "canceled", "incomplete", "incomplete_expired"}:
        return s
    return "incomplete"


def _entitlement_state_from_subscription(status: str) -> str:
    if status in {"trialing", "active"}:
        return "active"
    if status in {"past_due", "unpaid", "paused"}:
        return "grace"
    return "inactive"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _plan_rank_value(plan_code: Optional[str]) -> int:
    code = str(plan_code or "").strip().lower()
    if code.startswith("enterprise"):
        return 40 if "yearly" not in code else 41
    if code.startswith("business"):
        return 30 if "yearly" not in code else 31
    if code.startswith("pro"):
        return 20 if "yearly" not in code else 21
    if code.startswith("creator"):
        return 15
    return 10


def _strip_none_values(obj: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in obj.items() if v is not None}


def _build_pending_change(
    target_plan_code: str,
    effective_at: Optional[datetime],
    *,
    change_mode: str = "period_end",
    target_total_credits: Optional[int] = None,
) -> Dict[str, Any]:
    out = {
        "target_plan_code": _normalize_plan_code(target_plan_code),
        "effective_at": effective_at.isoformat() if effective_at else None,
        "change_mode": change_mode,
        "status": "scheduled",
    }
    if target_total_credits is not None:
        out["target_total_credits"] = int(target_total_credits)
    return out


async def clear_pending_change_metadata(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    gateway_subscription_id: Optional[str] = None,
) -> None:
    if gateway_subscription_id:
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
            gateway_subscription_id,
        )

    await conn.execute(
        """
        update pricing_user_entitlements
        set metadata_json = case
              when jsonb_typeof(metadata_json) = 'object' then metadata_json - 'pending_change'
              else '{}'::jsonb
            end
        where user_id = $1
        """,
        user_id,
    )

    await conn.execute(
        """
        update billing_entitlements
        set
          metadata_json = case
            when jsonb_typeof(metadata_json) = 'object' then metadata_json - 'pending_change'
            else '{}'::jsonb
          end,
          updated_at = now()
        where user_id = $1
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        """,
        user_id,
    )


@dataclass(frozen=True)
class IncludedCreditsPlan:
    tier_code: str
    plan_code: str
    included_credits_total: int
    overage_allowed: bool
    wallet_topup_allowed: bool
    hard_stop_on_insufficient_balance: bool


def _normalize_plan_code(plan_code: Optional[str], tier_code: Optional[str] = None) -> str:
    code = str(plan_code or "").strip().lower()
    tier = str(tier_code or "").strip().lower()
    if code:
        return code
    if tier == "pro":
        return "pro_monthly_v1"
    if tier == "business":
        return "business_monthly_v1"
    if tier == "enterprise":
        return "enterprise_monthly_v1"
    return "free"


def _resolve_plan_code(subscription: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    direct = _normalize_plan_code(str(metadata.get("df_plan_code") or "").strip())
    if direct and direct != "free":
        return direct

    items = _as_dict_loose(subscription.get("items"))
    data = items.get("data") or []
    if data:
        price = _as_dict_loose(_as_dict_loose(data[0]).get("price"))
        price_id = str(price.get("id") or "").strip()
        if price_id:
            mapped_row = _as_dict_loose(price.get("metadata"))
            mapped_code = _normalize_plan_code(mapped_row.get("df_plan_code"))
            if mapped_code and mapped_code != "free":
                return mapped_code

    return "free"


def _extract_subscription_period_bounds(subscription: Dict[str, Any]) -> tuple[Any, Any]:
    items = _as_dict_loose(subscription.get("items"))
    data = items.get("data") or []
    first_item = _as_dict_loose(data[0]) if data else {}

    start_raw = first_item.get("current_period_start")
    end_raw = first_item.get("current_period_end")

    if start_raw in (None, ""):
        start_raw = subscription.get("current_period_start")
    if end_raw in (None, ""):
        end_raw = subscription.get("current_period_end")

    return start_raw, end_raw


def _extract_latest_invoice_id(subscription: Dict[str, Any]) -> Optional[str]:
    latest_invoice = subscription.get("latest_invoice")
    if isinstance(latest_invoice, dict):
        return str(latest_invoice.get("id") or "").strip() or None
    return str(latest_invoice or "").strip() or None


async def _select_canonical_active_subscription_id(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
) -> Optional[str]:
    row = await conn.fetchrow(
        """
        select gateway_subscription_id
        from payment_plan_subscriptions
        where user_id = $1
          and gateway_subscription_id is not null
          and subscription_state in ('trialing', 'active', 'past_due', 'unpaid', 'paused')
          and entitlement_state in ('active', 'grace')
        order by
          case when cancel_at_period_end = false then 0 else 1 end,
          updated_at desc,
          created_at desc
        limit 1
        """,
        user_id,
    )
    return str(row["gateway_subscription_id"]) if row and row.get("gateway_subscription_id") else None


async def _load_plan_from_guardrails(
    conn: asyncpg.Connection,
    *,
    plan_code: str,
) -> IncludedCreditsPlan:
    normalized_code = _normalize_plan_code(plan_code)
    row = await conn.fetchrow(
        """
        select
          plan_code,
          tier_code,
          included_credit_cap,
          allow_topups
        from pricing_plan_credit_guardrails
        where plan_code = $1
          and is_active = true
        limit 1
        """,
        normalized_code,
    )
    if not row:
        if normalized_code == "free":
            return IncludedCreditsPlan(
                tier_code="free",
                plan_code="free",
                included_credits_total=0,
                overage_allowed=False,
                wallet_topup_allowed=False,
                hard_stop_on_insufficient_balance=True,
            )
        raise RuntimeError(f"pricing_plan_credit_guardrails_missing:{normalized_code}")

    tier_code = str(row["tier_code"] or "free").strip().lower()
    included_credit_cap = _safe_int(row["included_credit_cap"], 0)
    allow_topups = bool(row["allow_topups"])

    return IncludedCreditsPlan(
        tier_code=tier_code,
        plan_code=str(row["plan_code"]).strip().lower(),
        included_credits_total=max(0, included_credit_cap),
        overage_allowed=False,
        wallet_topup_allowed=allow_topups,
        hard_stop_on_insufficient_balance=True,
    )


def _effective_tier_code(plan: IncludedCreditsPlan, entitlement_state: str) -> str:
    return plan.tier_code if entitlement_state in {"active", "grace"} else "free"



async def _grant_cycle_credits_once(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    plan: IncludedCreditsPlan,
    cycle_key: Optional[str],
    gateway_subscription_id: str,
    billing_entitlement_metadata: Dict[str, Any],
) -> tuple[int, Optional[str]]:
    if plan.included_credits_total <= 0 or not cycle_key:
        return 0, None

    md = _as_dict_loose(billing_entitlement_metadata)
    if str(md.get("last_granted_cycle_key") or "") == cycle_key:
        return 0, str(md.get("last_grant_ledger_entry_id") or "") or None

    await _ensure_account_row(conn, user_id)

    lot_ref = f"subscription_cycle:{cycle_key}"
    existing_cycle_lot = await conn.fetchrow(
        """
        select id
        from pricing_credit_lots
        where user_id = $1
          and bucket_type = 'included'
          and source_type = 'plan_grant'
          and source_ref = $2
          and status = 'active'
        limit 1
        """,
        user_id,
        lot_ref,
    )
    if existing_cycle_lot:
        return 0, str(md.get("last_grant_ledger_entry_id") or "") or None

    current_included_row = await conn.fetchrow(
        """
        select coalesce(sum(case when bucket_type = 'included' and status = 'active' then remaining_amount else 0 end), 0) as included_remaining
        from pricing_credit_lots
        where user_id = $1
        """,
        user_id,
    )
    current_included_remaining = _safe_int(current_included_row["included_remaining"], 0) if current_included_row else 0
    credits_to_grant = max(0, plan.included_credits_total - current_included_remaining)
    if credits_to_grant <= 0:
        return 0, None

    insert_status = await conn.execute(
        """
        insert into pricing_credit_lots(
          id,
          billing_account_id,
          user_id,
          bucket_type,
          source_type,
          source_ref,
          plan_code_at_grant,
          granted_amount,
          remaining_amount,
          reserved_amount,
          granted_at,
          expires_at,
          status,
          metadata_json,
          created_at,
          updated_at
        )
        select
          gen_random_uuid(),
          null::uuid,
          $1::uuid,
          'included'::text,
          'plan_grant'::text,
          $2::text,
          $3::text,
          $4::numeric,
          $4::numeric,
          0::numeric,
          now(),
          null::timestamptz,
          'active'::text,
          jsonb_build_object(
            'source', 'stripe_subscription',
            'gateway_subscription_id', $5::text,
            'cycle_key', $6::text,
            'plan_code', $3::text,
            'granted_credits', $4::numeric,
            'target_cap', $7::numeric,
            'current_included_remaining_before', $8::numeric
          ),
          now(),
          now()
        where not exists (
          select 1
          from pricing_credit_lots
          where user_id = $1::uuid
            and bucket_type = 'included'
            and source_type = 'plan_grant'
            and source_ref = $2::text
            and status = 'active'
        )
        """,
        user_id,
        lot_ref,
        plan.plan_code,
        credits_to_grant,
        gateway_subscription_id,
        cycle_key,
        plan.included_credits_total,
        current_included_remaining,
    )
    if not str(insert_status).endswith("1"):
        return 0, str(md.get("last_grant_ledger_entry_id") or "") or None

    await conn.execute(
        "update pricing_credit_accounts set balance_credits = balance_credits + $2, updated_at = now() where user_id = $1",
        user_id,
        credits_to_grant,
    )

    ledger_key = f"subscription_cycle_grant:{gateway_subscription_id}:{cycle_key}"
    await _ledger_event(
        conn,
        user_id=user_id,
        event_type="subscription_cycle_grant",
        credits_delta=credits_to_grant,
        idempotency_key=ledger_key,
        sku_code=None,
        quantity=Decimal("1"),
        country_code=None,
        currency="USD",
        money_amount=None,
        channel="service",
        metadata={
            "gateway_subscription_id": gateway_subscription_id,
            "plan_code": plan.plan_code,
            "cycle_key": cycle_key,
            "granted_credits": credits_to_grant,
            "target_cap": plan.included_credits_total,
            "current_included_remaining_before": current_included_remaining,
            "source_of_truth": "pricing_plan_credit_guardrails",
        },
        billing_account_id=None,
        settlement_mode="prepaid",
        reservation_id=None,
        studio_job_id=None,
        service_name="svc-pricing",
        service_action="subscription_cycle_grant",
    )
    ledger_row = await conn.fetchrow(
        "select id from pricing_credit_ledger_events where user_id = $1 and idempotency_key = $2 limit 1",
        user_id,
        ledger_key,
    )
    return credits_to_grant, (str(ledger_row["id"]) if ledger_row else None)


async def _best_effort_sync_core_user_tier(conn: asyncpg.Connection, *, user_id: UUID, tier_code: str) -> None:
    try:
        await conn.execute("update core.users set tier = $2 where id = $1", user_id, tier_code)
    except Exception:
        pass


async def sync_subscription_and_entitlement(
    conn: asyncpg.Connection,
    *,
    subscription: Dict[str, Any],
    fallback_user_id: Optional[str] = None,
    latest_invoice_status: Optional[str] = None,
) -> Optional[UUID]:
    metadata = _as_dict_loose(subscription.get("metadata"))
    user_id = _to_uuid_or_none(metadata.get("df_user_id") or fallback_user_id)
    if not user_id:
        return None

    gateway_customer_id = str(subscription.get("customer") or "").strip() or None
    gateway_subscription_id = str(subscription.get("id") or "").strip()
    if not gateway_subscription_id:
        return user_id

    status = _normalize_subscription_state(subscription.get("status"))
    entitlement_state = _entitlement_state_from_subscription(status)
    cancel_at_period_end = bool(subscription.get("cancel_at_period_end") or False)

    current_period_start_raw, current_period_end_raw = _extract_subscription_period_bounds(subscription)
    current_period_start_dt = _to_datetime_or_none(current_period_start_raw)
    current_period_end_dt = _to_datetime_or_none(current_period_end_raw)

    if entitlement_state == "active" and (
        current_period_start_raw in (None, "") or current_period_end_raw in (None, "")
    ):
        raise RuntimeError(f"stripe_subscription_missing_billing_period:{gateway_subscription_id}")

    price_id = None
    items = _as_dict_loose(subscription.get("items"))
    data = items.get("data") or []
    if data:
        price = _as_dict_loose(_as_dict_loose(data[0]).get("price"))
        price_id = str(price.get("id") or "").strip() or None

    incoming_plan_code = _resolve_plan_code(subscription, metadata)
    incoming_plan = await _load_plan_from_guardrails(conn, plan_code=incoming_plan_code)

    cycle_key = None
    if current_period_start_raw is not None and current_period_end_raw is not None:
        cycle_key = f"{gateway_subscription_id}:{current_period_start_raw}:{current_period_end_raw}"

    existing_ent_row = await conn.fetchrow(
        "select metadata_json from billing_entitlements where user_id = $1 for update",
        user_id,
    )
    existing_ent = await _BILLING_ENTITLEMENTS_REPO.get_active_by_user_id(conn, user_id=user_id)
    ent_md = _as_dict_loose(existing_ent_row["metadata_json"]) if existing_ent_row else (
        existing_ent.metadata_json if existing_ent else {}
    )
    existing_user_ent = await _ENTITLEMENTS_REPO.get_user_entitlement(conn, user_id=user_id)
    existing_sub_row = await _PAYMENT_PLAN_SUBSCRIPTIONS_REPO.get_by_gateway_subscription_id(
        conn,
        gateway_subscription_id=gateway_subscription_id,
    )
    existing_sub_md = existing_sub_row.metadata_json if existing_sub_row else {}
    existing_pending_change = _as_dict_loose(existing_sub_md.get("pending_change")) or _as_dict_loose(ent_md.get("pending_change"))

    existing_plan_code = str(existing_ent.plan_code or "").strip().lower() if existing_ent else ""
    existing_plan_rank = _plan_rank_value(existing_plan_code)
    incoming_plan_rank = _plan_rank_value(incoming_plan.plan_code)

    effective_plan = incoming_plan
    pending_change: Optional[Dict[str, Any]] = None

    if (
        entitlement_state in {"active", "grace"}
        and existing_ent
        and existing_plan_code not in {"", "free"}
        and existing_plan_rank > incoming_plan_rank
        and current_period_end_dt is not None
        and current_period_end_dt > _now()
    ):
        effective_plan = await _load_plan_from_guardrails(conn, plan_code=existing_plan_code)
        pending_change = _build_pending_change(
            incoming_plan.plan_code,
            current_period_end_dt,
            target_total_credits=incoming_plan.included_credits_total,
        )

    if cancel_at_period_end:
        specific_existing_target = _normalize_plan_code(existing_pending_change.get("target_plan_code")) if existing_pending_change else ""
        if specific_existing_target and specific_existing_target != "free":
            pending_change = _strip_none_values({
                "target_plan_code": specific_existing_target,
                "effective_at": existing_pending_change.get("effective_at") or (current_period_end_dt.isoformat() if current_period_end_dt else None),
                "change_mode": existing_pending_change.get("change_mode") or "period_end",
                "status": existing_pending_change.get("status") or "scheduled",
                "target_total_credits": (
                    _safe_int(existing_pending_change.get("target_total_credits"), 0)
                    if existing_pending_change.get("target_total_credits") is not None
                    else None
                ),
            })
        elif pending_change is None:
            pending_change = _build_pending_change("free", current_period_end_dt, target_total_credits=100)

    subscription_md = {
        **_as_dict_loose(subscription),
        "effective_plan_code": effective_plan.plan_code if entitlement_state in {"active", "grace"} else "free",
        "effective_tier_code": _effective_tier_code(effective_plan, entitlement_state),
        "latest_invoice_status": latest_invoice_status,
    }
    if pending_change is not None:
        subscription_md["pending_change"] = pending_change

    await _PAYMENT_PLAN_SUBSCRIPTIONS_REPO.upsert_from_gateway_subscription(
        conn,
        user_id=user_id,
        gateway_provider="stripe",
        gateway_customer_id=gateway_customer_id,
        gateway_subscription_id=gateway_subscription_id,
        gateway_price_id=price_id,
        plan_code=incoming_plan.plan_code,
        subscription_state=status,
        current_period_start=current_period_start_dt,
        current_period_end=current_period_end_dt,
        cancel_at_period_end=cancel_at_period_end,
        latest_invoice_id=_extract_latest_invoice_id(subscription),
        latest_invoice_status=str(latest_invoice_status or "").strip() or None,
        entitlement_state=entitlement_state,
        metadata_json=subscription_md,
    )

    canonical_subscription_id = await _select_canonical_active_subscription_id(conn, user_id=user_id)
    if entitlement_state in {"active", "grace"} and canonical_subscription_id and canonical_subscription_id != gateway_subscription_id:
        return user_id

    effective_tier_code = _effective_tier_code(effective_plan, entitlement_state)
    previous_remaining = int(existing_ent.included_credits_remaining) if existing_ent else 0
    same_plan = bool(existing_ent and existing_ent.plan_code == effective_plan.plan_code)

    pregrant_md = {
        **ent_md,
        "gateway_provider": "stripe",
        "gateway_subscription_id": gateway_subscription_id,
        "gateway_customer_id": gateway_customer_id,
        "subscription_state": status,
        "entitlement_state": entitlement_state,
        "plan_code": effective_plan.plan_code,
        "price_id": price_id,
        "latest_invoice_status": latest_invoice_status,
        "current_period_start": _to_iso_or_none(current_period_start_raw),
        "current_period_end": _to_iso_or_none(current_period_end_raw),
        "cycle_key": cycle_key,
        "plan_credit_source": "pricing_plan_credit_guardrails",
        "sync_stage": "pregrant",
    }
    if pending_change is not None:
        pregrant_md["pending_change"] = pending_change

    if entitlement_state in {"active", "grace"}:
        await _BILLING_ENTITLEMENTS_REPO.upsert_billing_entitlement(
            conn,
            user_id=user_id,
            tier_code=effective_tier_code,
            plan_code=effective_plan.plan_code,
            billing_mode="subscription",
            settlement_mode="credits",
            included_credits_total=effective_plan.included_credits_total,
            included_credits_remaining=max(0, int(previous_remaining if same_plan else 0)),
            overage_allowed=effective_plan.overage_allowed,
            wallet_topup_allowed=effective_plan.wallet_topup_allowed,
            hard_stop_on_insufficient_balance=effective_plan.hard_stop_on_insufficient_balance,
            source="stripe_subscription",
            metadata_json=pregrant_md,
        )
        merged_user_md = {
            **(existing_user_ent.metadata_json if existing_user_ent else {}),
            "source": "stripe_subscription",
            "entitlement_state": entitlement_state,
            "plan_code": effective_plan.plan_code,
            "gateway_provider": "stripe",
            "gateway_subscription_id": gateway_subscription_id,
            "gateway_customer_id": gateway_customer_id,
            "latest_invoice_status": latest_invoice_status,
            "plan_credit_source": "pricing_plan_credit_guardrails",
        }
        if pending_change is not None:
            merged_user_md["pending_change"] = pending_change
        await _ENTITLEMENTS_REPO.upsert_user_entitlement(
            conn,
            user_id=user_id,
            tier_code=effective_tier_code,
            billing_account_id=(existing_user_ent.billing_account_id if existing_user_ent else None),
            metadata_json=merged_user_md,
        )
        await _best_effort_sync_core_user_tier(conn, user_id=user_id, tier_code=effective_tier_code)

    granted_credits = 0
    ledger_entry_id = None
    if entitlement_state == "active":
        granted_credits, ledger_entry_id = await _grant_cycle_credits_once(
            conn,
            user_id=user_id,
            plan=effective_plan,
            cycle_key=cycle_key,
            gateway_subscription_id=gateway_subscription_id,
            billing_entitlement_metadata=ent_md,
        )

    updated_md = {
        **ent_md,
        "gateway_provider": "stripe",
        "gateway_subscription_id": gateway_subscription_id,
        "gateway_customer_id": gateway_customer_id,
        "subscription_state": status,
        "entitlement_state": entitlement_state,
        "plan_code": effective_plan.plan_code if entitlement_state in {"active", "grace"} else None,
        "price_id": price_id,
        "latest_invoice_status": latest_invoice_status,
        "current_period_start": _to_iso_or_none(current_period_start_raw),
        "current_period_end": _to_iso_or_none(current_period_end_raw),
        "cycle_key": cycle_key,
        "plan_credit_source": "pricing_plan_credit_guardrails",
        "sync_stage": "final",
    }
    if pending_change is not None:
        updated_md["pending_change"] = pending_change
    if granted_credits > 0:
        updated_md["last_granted_cycle_key"] = cycle_key
        updated_md["last_granted_at"] = _now().isoformat()
        updated_md["last_granted_credits"] = granted_credits
        updated_md["last_grant_ledger_entry_id"] = ledger_entry_id

    if entitlement_state == "active":
        if granted_credits > 0:
            included_remaining = effective_plan.included_credits_total
        elif existing_ent and same_plan:
            included_remaining = previous_remaining
        else:
            included_remaining = previous_remaining
            updated_md["grant_status"] = "pending_sync"
    elif entitlement_state == "grace":
        included_remaining = previous_remaining
    else:
        included_remaining = 0

    await _BILLING_ENTITLEMENTS_REPO.upsert_billing_entitlement(
        conn,
        user_id=user_id,
        tier_code=effective_tier_code,
        plan_code=effective_plan.plan_code if entitlement_state in {"active", "grace"} else None,
        billing_mode="subscription" if entitlement_state in {"active", "grace"} else "free",
        settlement_mode="credits",
        included_credits_total=effective_plan.included_credits_total if entitlement_state in {"active", "grace"} else 0,
        included_credits_remaining=max(0, int(included_remaining)),
        overage_allowed=effective_plan.overage_allowed if entitlement_state in {"active", "grace"} else False,
        wallet_topup_allowed=effective_plan.wallet_topup_allowed if entitlement_state in {"active", "grace"} else True,
        hard_stop_on_insufficient_balance=effective_plan.hard_stop_on_insufficient_balance if entitlement_state in {"active", "grace"} else True,
        source="stripe_subscription",
        metadata_json=updated_md,
    )

    merged_user_md = {
        **(existing_user_ent.metadata_json if existing_user_ent else {}),
        "source": "stripe_subscription",
        "entitlement_state": entitlement_state,
        "plan_code": effective_plan.plan_code if entitlement_state in {"active", "grace"} else "free",
        "gateway_provider": "stripe",
        "gateway_subscription_id": gateway_subscription_id,
        "gateway_customer_id": gateway_customer_id,
        "latest_invoice_status": latest_invoice_status,
        "plan_credit_source": "pricing_plan_credit_guardrails",
    }
    if pending_change is not None:
        merged_user_md["pending_change"] = pending_change
    await _ENTITLEMENTS_REPO.upsert_user_entitlement(
        conn,
        user_id=user_id,
        tier_code=effective_tier_code,
        billing_account_id=(existing_user_ent.billing_account_id if existing_user_ent else None),
        metadata_json=merged_user_md,
    )

    await _best_effort_sync_core_user_tier(conn, user_id=user_id, tier_code=effective_tier_code)
    return user_id


    status = _normalize_subscription_state(subscription.get("status"))
    entitlement_state = _entitlement_state_from_subscription(status)
    cancel_at_period_end = bool(subscription.get("cancel_at_period_end") or False)

    current_period_start_raw, current_period_end_raw = _extract_subscription_period_bounds(subscription)
    current_period_start_dt = _to_datetime_or_none(current_period_start_raw)
    current_period_end_dt = _to_datetime_or_none(current_period_end_raw)

    if entitlement_state == "active" and (
        current_period_start_raw in (None, "") or current_period_end_raw in (None, "")
    ):
        raise RuntimeError(f"stripe_subscription_missing_billing_period:{gateway_subscription_id}")

    price_id = None
    items = _as_dict_loose(subscription.get("items"))
    data = items.get("data") or []
    if data:
        price = _as_dict_loose(_as_dict_loose(data[0]).get("price"))
        price_id = str(price.get("id") or "").strip() or None

    plan_code = _resolve_plan_code(subscription, metadata)
    plan = await _load_plan_from_guardrails(conn, plan_code=plan_code)

    await _PAYMENT_PLAN_SUBSCRIPTIONS_REPO.upsert_from_gateway_subscription(
        conn,
        user_id=user_id,
        gateway_provider="stripe",
        gateway_customer_id=gateway_customer_id,
        gateway_subscription_id=gateway_subscription_id,
        gateway_price_id=price_id,
        plan_code=plan.plan_code,
        subscription_state=status,
        current_period_start=current_period_start_dt,
        current_period_end=current_period_end_dt,
        cancel_at_period_end=cancel_at_period_end,
        latest_invoice_id=_extract_latest_invoice_id(subscription),
        latest_invoice_status=str(latest_invoice_status or "").strip() or None,
        entitlement_state=entitlement_state,
        metadata_json=subscription,
    )

    canonical_subscription_id = await _select_canonical_active_subscription_id(
        conn,
        user_id=user_id,
    )

    if entitlement_state in {"active", "grace"}:
        if canonical_subscription_id and canonical_subscription_id != gateway_subscription_id:
            return user_id

    effective_tier_code = _effective_tier_code(plan, entitlement_state)

    cycle_key = None
    if current_period_start_raw is not None and current_period_end_raw is not None:
        cycle_key = f"{gateway_subscription_id}:{current_period_start_raw}:{current_period_end_raw}"

    existing_ent_row = await conn.fetchrow(
        "select metadata_json from billing_entitlements where user_id = $1 for update",
        user_id,
    )
    existing_ent = await _BILLING_ENTITLEMENTS_REPO.get_active_by_user_id(conn, user_id=user_id)
    ent_md = _as_dict_loose(existing_ent_row["metadata_json"]) if existing_ent_row else (
        existing_ent.metadata_json if existing_ent else {}
    )

    granted_credits = 0
    ledger_entry_id = None
    if entitlement_state == "active":
        granted_credits, ledger_entry_id = await _grant_cycle_credits_once(
            conn,
            user_id=user_id,
            plan=plan,
            cycle_key=cycle_key,
            gateway_subscription_id=gateway_subscription_id,
            billing_entitlement_metadata=ent_md,
        )

    updated_md = {
        **ent_md,
        "gateway_provider": "stripe",
        "gateway_subscription_id": gateway_subscription_id,
        "gateway_customer_id": gateway_customer_id,
        "subscription_state": status,
        "entitlement_state": entitlement_state,
        "plan_code": plan.plan_code,
        "price_id": price_id,
        "latest_invoice_status": latest_invoice_status,
        "current_period_start": _to_iso_or_none(current_period_start_raw),
        "current_period_end": _to_iso_or_none(current_period_end_raw),
        "cycle_key": cycle_key,
        "plan_credit_source": "pricing_plan_credit_guardrails",
    }
    if granted_credits > 0:
        updated_md["last_granted_cycle_key"] = cycle_key
        updated_md["last_granted_at"] = _now().isoformat()
        updated_md["last_granted_credits"] = granted_credits
        updated_md["last_grant_ledger_entry_id"] = ledger_entry_id

    previous_remaining = int(existing_ent.included_credits_remaining) if existing_ent else 0
    same_plan = bool(existing_ent and existing_ent.plan_code == plan.plan_code)

    if entitlement_state == "active":
        if granted_credits > 0:
            included_remaining = plan.included_credits_total
        elif existing_ent and same_plan:
            included_remaining = previous_remaining
        else:
            included_remaining = previous_remaining
            updated_md["grant_status"] = "pending_sync"
    elif entitlement_state == "grace":
        included_remaining = previous_remaining
    else:
        included_remaining = 0

    await _BILLING_ENTITLEMENTS_REPO.upsert_billing_entitlement(
        conn,
        user_id=user_id,
        tier_code=effective_tier_code,
        plan_code=plan.plan_code if entitlement_state in {"active", "grace"} else None,
        billing_mode="subscription" if entitlement_state in {"active", "grace"} else "free",
        settlement_mode="credits",
        included_credits_total=plan.included_credits_total if entitlement_state in {"active", "grace"} else 0,
        included_credits_remaining=max(0, int(included_remaining)),
        overage_allowed=plan.overage_allowed if entitlement_state in {"active", "grace"} else False,
        wallet_topup_allowed=plan.wallet_topup_allowed if entitlement_state in {"active", "grace"} else True,
        hard_stop_on_insufficient_balance=plan.hard_stop_on_insufficient_balance if entitlement_state in {"active", "grace"} else True,
        source="stripe_subscription",
        metadata_json=updated_md,
    )

    existing_user_ent = await _ENTITLEMENTS_REPO.get_user_entitlement(conn, user_id=user_id)
    merged_user_md = {
        **(existing_user_ent.metadata_json if existing_user_ent else {}),
        "source": "stripe_subscription",
        "entitlement_state": entitlement_state,
        "plan_code": plan.plan_code,
        "gateway_provider": "stripe",
        "gateway_subscription_id": gateway_subscription_id,
        "gateway_customer_id": gateway_customer_id,
        "latest_invoice_status": latest_invoice_status,
        "plan_credit_source": "pricing_plan_credit_guardrails",
    }
    await _ENTITLEMENTS_REPO.upsert_user_entitlement(
        conn,
        user_id=user_id,
        tier_code=effective_tier_code,
        billing_account_id=(existing_user_ent.billing_account_id if existing_user_ent else None),
        metadata_json=merged_user_md,
    )

    await _best_effort_sync_core_user_tier(conn, user_id=user_id, tier_code=effective_tier_code)
    return user_id
