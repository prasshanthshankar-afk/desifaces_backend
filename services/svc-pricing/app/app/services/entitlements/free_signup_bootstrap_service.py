from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.repo.billing_entitlements_repo import BillingEntitlementsRepo
from app.repo.entitlements_repo import EntitlementsRepo

_ENTITLEMENTS_REPO = EntitlementsRepo()
_BILLING_ENTITLEMENTS_REPO = BillingEntitlementsRepo()


def _free_signup_credits_default() -> int:
    raw = os.getenv("DF_FREE_SIGNUP_CREDITS", "100")
    try:
        value = int(raw)
        return value if value > 0 else 100
    except Exception:
        return 100


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _safe_text(value: Any) -> str:
    return str(value or "").strip().lower()


async def _ensure_core_user_tier_free(conn: asyncpg.Connection, *, user_id: UUID, current_tier: str) -> str:
    if current_tier not in {"", "free"}:
        return "preserved_existing"

    if current_tier == "free":
        return "preserved_existing"

    await conn.execute(
        """
        update core.users
        set tier = 'free'
        where id = $1
          and coalesce(trim(tier), '') = ''
        """,
        user_id,
    )
    return "set_to_free"


async def _fetch_active_lot_summary(conn: asyncpg.Connection, *, user_id: UUID) -> Dict[str, int]:
    row = await conn.fetchrow(
        """
        select
          count(*) filter (where status = 'active') as active_lot_count,
          coalesce(sum(case when bucket_type = 'included' and status = 'active' then granted_amount else 0 end), 0) as included_granted,
          coalesce(sum(case when bucket_type = 'included' and status = 'active' then remaining_amount else 0 end), 0) as included_remaining,
          coalesce(sum(case when bucket_type = 'included' and status = 'active' then reserved_amount else 0 end), 0) as included_reserved
        from pricing_credit_lots
        where user_id = $1
        """,
        user_id,
    )
    if not row:
        return {
            "active_lot_count": 0,
            "included_granted": 0,
            "included_remaining": 0,
            "included_reserved": 0,
        }
    return {
        "active_lot_count": _safe_int(row.get("active_lot_count"), 0),
        "included_granted": _safe_int(row.get("included_granted"), 0),
        "included_remaining": _safe_int(row.get("included_remaining"), 0),
        "included_reserved": _safe_int(row.get("included_reserved"), 0),
    }


async def _fetch_credit_account(conn: asyncpg.Connection, *, user_id: UUID) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        """
        select user_id, balance_credits, reserved_credits, billing_account_id, settlement_mode
        from pricing_credit_accounts
        where user_id = $1
        """,
        user_id,
    )


async def _insert_or_sync_free_plan_grant_lot(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    email: Optional[str],
    source: str,
    target_credits: int,
    existing_bill: Optional[Any],
) -> str:
    lot_summary = await _fetch_active_lot_summary(conn, user_id=user_id)
    if lot_summary["active_lot_count"] > 0:
        return "preserved_existing"

    account = await _fetch_credit_account(conn, user_id=user_id)
    balance_credits = _safe_int(account.get("balance_credits"), 0) if account else 0
    reserved_credits = _safe_int(account.get("reserved_credits"), 0) if account else 0
    billing_account_id = account.get("billing_account_id") if account else None
    settlement_mode = _safe_text(account.get("settlement_mode") if account else None)

    if billing_account_id is not None:
        return "skipped_existing_billing_account"

    if settlement_mode not in {"", "prepaid", "credits"}:
        return "skipped_non_prepaid_account"

    existing_total = _safe_int(existing_bill.included_credits_total, 0) if existing_bill else 0
    existing_remaining = _safe_int(existing_bill.included_credits_remaining, 0) if existing_bill else 0

    granted_amount = max(existing_total, target_credits)
    if granted_amount <= 0:
        granted_amount = target_credits

    remaining_amount = existing_remaining if existing_remaining > 0 else balance_credits
    if remaining_amount <= 0 and balance_credits == 0 and reserved_credits == 0:
        remaining_amount = granted_amount

    if remaining_amount > granted_amount:
        return "skipped_balance_exceeds_free_grant"

    reserved_amount = max(0, min(reserved_credits, remaining_amount))

    await conn.execute(
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
          'free'::text,
          $3::numeric,
          $4::numeric,
          $5::numeric,
          now(),
          null::timestamptz,
          'active'::text,
          jsonb_build_object(
            'source', $2::text,
            'email', $6::text,
            'bootstrap_kind', 'free_signup',
            'bootstrap_applied', true
          ),
          now(),
          now()
        where not exists (
          select 1
          from pricing_credit_lots
          where user_id = $1::uuid
            and status = 'active'
        )
        """,
        user_id,
        source,
        granted_amount,
        remaining_amount,
        reserved_amount,
        email,
    )
    return "created"


async def _reconcile_free_billing_entitlement(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    email: Optional[str],
    source: str,
    target_credits: int,
) -> Any:
    lot_summary = await _fetch_active_lot_summary(conn, user_id=user_id)
    account = await _fetch_credit_account(conn, user_id=user_id)

    included_granted = lot_summary["included_granted"]
    included_remaining = lot_summary["included_remaining"]
    account_balance = _safe_int(account.get("balance_credits"), 0) if account else 0

    final_total = included_granted if included_granted > 0 else target_credits
    final_remaining = included_remaining if included_remaining > 0 else account_balance
    if final_remaining <= 0:
        final_remaining = min(account_balance if account_balance > 0 else final_total, final_total)
    final_remaining = max(0, min(final_remaining, final_total))

    return await _BILLING_ENTITLEMENTS_REPO.upsert_billing_entitlement(
        conn,
        user_id=user_id,
        tier_code="free",
        plan_code="free",
        billing_mode="free",
        settlement_mode="credits",
        included_credits_total=final_total,
        included_credits_remaining=final_remaining,
        overage_allowed=False,
        wallet_topup_allowed=True,
        hard_stop_on_insufficient_balance=True,
        source=source,
        metadata_json={
            "source": source,
            "email": email,
            "bootstrap_kind": "free_signup",
            "bootstrap_applied": True,
            "reconciled_from": "lots_and_account",
        },
    )


async def bootstrap_free_user_pricing(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    email: Optional[str] = None,
    source: str = "free_signup_bootstrap",
    credits: Optional[int] = None,
) -> Dict[str, Any]:
    target_credits = int(credits if credits is not None else _free_signup_credits_default())
    if target_credits <= 0:
        target_credits = 100

    core_user = await conn.fetchrow(
        """
        select id, email, tier
        from core.users
        where id = $1
        """,
        user_id,
    )
    if not core_user:
        return {"bootstrapped": False, "reason": "core_user_not_found"}

    core_tier = _safe_text(core_user.get("tier"))
    effective_email = email or str(core_user.get("email") or "").strip() or None

    existing_ent = await _ENTITLEMENTS_REPO.get_user_entitlement(conn, user_id=user_id)
    existing_bill = await _BILLING_ENTITLEMENTS_REPO.get_active_by_user_id(conn, user_id=user_id)
    existing_account = await _fetch_credit_account(conn, user_id=user_id)

    if existing_ent and _safe_text(existing_ent.tier_code) not in {"", "free"}:
        return {"bootstrapped": False, "reason": "existing_non_free_user_entitlement"}
    if existing_bill and _safe_text(existing_bill.tier_code) not in {"", "free"}:
        return {"bootstrapped": False, "reason": "existing_non_free_billing_entitlement"}
    if existing_bill and _safe_text(existing_bill.billing_mode) not in {"", "free"}:
        return {"bootstrapped": False, "reason": "existing_non_free_billing_mode"}

    if core_tier not in {"", "free"} and not existing_ent and not existing_bill:
        return {"bootstrapped": False, "reason": f"core_tier_not_free:{core_tier}"}

    core_tier_action = await _ensure_core_user_tier_free(
        conn,
        user_id=user_id,
        current_tier=core_tier,
    )

    ent = await _ENTITLEMENTS_REPO.ensure_default_free_entitlement(
        conn,
        user_id=user_id,
        metadata_json={
            "source": source,
            "email": effective_email,
            "bootstrap_kind": "free_signup",
            "bootstrap_applied": True,
        },
    )

    existing_total = _safe_int(existing_bill.included_credits_total, 0) if existing_bill else 0
    existing_remaining = _safe_int(existing_bill.included_credits_remaining, 0) if existing_bill else 0
    account_balance = _safe_int(existing_account.get("balance_credits"), 0) if existing_account else 0

    desired_total = existing_total if existing_total > 0 else target_credits
    desired_remaining = existing_remaining if existing_remaining > 0 else account_balance
    if desired_remaining <= 0:
        desired_remaining = target_credits if account_balance == 0 else account_balance
    desired_remaining = max(0, min(desired_remaining, desired_total))

    if existing_bill is None:
        bill = await _BILLING_ENTITLEMENTS_REPO.upsert_billing_entitlement(
            conn,
            user_id=user_id,
            tier_code="free",
            plan_code="free",
            billing_mode="free",
            settlement_mode="credits",
            included_credits_total=desired_total,
            included_credits_remaining=desired_remaining,
            overage_allowed=False,
            wallet_topup_allowed=True,
            hard_stop_on_insufficient_balance=True,
            source=source,
            metadata_json={
                "source": source,
                "email": effective_email,
                "bootstrap_kind": "free_signup",
                "bootstrap_applied": True,
            },
        )
        billing_entitlement_action = "created"
    else:
        existing_plan_code = _safe_text(existing_bill.plan_code)
        existing_billing_mode = _safe_text(existing_bill.billing_mode)
        existing_settlement_mode = _safe_text(existing_bill.settlement_mode)

        if (
            existing_total <= 0
            and existing_remaining <= 0
            and existing_plan_code in {"", "free"}
            and existing_billing_mode in {"", "free"}
            and existing_settlement_mode in {"", "credits"}
        ):
            bill = await _BILLING_ENTITLEMENTS_REPO.upsert_billing_entitlement(
                conn,
                user_id=user_id,
                tier_code="free",
                plan_code="free",
                billing_mode="free",
                settlement_mode="credits",
                included_credits_total=desired_total,
                included_credits_remaining=desired_remaining,
                overage_allowed=False,
                wallet_topup_allowed=True,
                hard_stop_on_insufficient_balance=True,
                source=source,
                metadata_json={
                    "source": source,
                    "email": effective_email,
                    "bootstrap_kind": "free_signup",
                    "bootstrap_applied": True,
                },
            )
            billing_entitlement_action = "uplifted_zero_balance"
        else:
            bill = existing_bill
            billing_entitlement_action = "preserved_existing"

    if existing_account is None:
        await conn.execute(
            """
            insert into pricing_credit_accounts(
              user_id, balance_credits, reserved_credits, settlement_mode, updated_at
            )
            values($1, $2, 0, 'prepaid', now())
            on conflict (user_id) do nothing
            """,
            user_id,
            desired_remaining,
        )
        account_action = "created"
    else:
        balance_credits = _safe_int(existing_account.get("balance_credits"), 0)
        reserved_credits = _safe_int(existing_account.get("reserved_credits"), 0)
        billing_account_id = existing_account.get("billing_account_id")
        settlement_mode = _safe_text(existing_account.get("settlement_mode"))

        if (
            balance_credits == 0
            and reserved_credits == 0
            and billing_account_id is None
            and settlement_mode in {"", "credits", "prepaid"}
        ):
            await conn.execute(
                """
                update pricing_credit_accounts
                set balance_credits = $2,
                    reserved_credits = 0,
                    settlement_mode = case
                        when coalesce(trim(settlement_mode), '') in ('', 'credits') then 'prepaid'
                        else settlement_mode
                    end,
                    updated_at = now()
                where user_id = $1
                """,
                user_id,
                desired_remaining,
            )
            account_action = "uplifted_zero_balance"
        else:
            account_action = "preserved_existing"

    current_bill = await _BILLING_ENTITLEMENTS_REPO.get_active_by_user_id(conn, user_id=user_id)
    lot_action = await _insert_or_sync_free_plan_grant_lot(
        conn,
        user_id=user_id,
        email=effective_email,
        source=source,
        target_credits=target_credits,
        existing_bill=current_bill,
    )

    reconciled_bill = await _reconcile_free_billing_entitlement(
        conn,
        user_id=user_id,
        email=effective_email,
        source=source,
        target_credits=target_credits,
    )

    lot_summary = await _fetch_active_lot_summary(conn, user_id=user_id)

    return {
        "bootstrapped": True,
        "user_id": str(user_id),
        "tier_code": ent.tier_code,
        "plan_code": reconciled_bill.plan_code,
        "included_credits_total": reconciled_bill.included_credits_total,
        "included_credits_remaining": reconciled_bill.included_credits_remaining,
        "target_credits": target_credits,
        "core_tier_action": core_tier_action,
        "billing_entitlement_action": billing_entitlement_action,
        "account_action": account_action,
        "lot_action": lot_action,
        "billing_entitlement_reconciled": True,
        "active_lot_count": lot_summary["active_lot_count"],
        "included_granted": lot_summary["included_granted"],
        "included_remaining": lot_summary["included_remaining"],
        "included_reserved": lot_summary["included_reserved"],
    }
