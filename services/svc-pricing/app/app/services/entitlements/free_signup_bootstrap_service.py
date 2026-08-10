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


_ACTIVE_PAID_SUBSCRIPTION_STATES = {
    "active",
    "trialing",
    "past_due",
    "incomplete",
    "incomplete_expired",
    "pending",
}


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


def _as_bool(value: Any, default: bool = False) -> bool:
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


async def _table_exists(conn: asyncpg.Connection, regclass_name: str) -> bool:
    try:
        row = await conn.fetchrow("select to_regclass($1) as reg", regclass_name)
        return bool(row and row.get("reg"))
    except Exception:
        return False


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
          count(*) filter (where status = 'active' and (expires_at is null or expires_at > now())) as active_lot_count,
          coalesce(sum(case when bucket_type = 'included' and status = 'active' and (expires_at is null or expires_at > now()) then granted_amount else 0 end), 0) as included_granted,
          coalesce(sum(case when bucket_type = 'included' and status = 'active' and (expires_at is null or expires_at > now()) then remaining_amount else 0 end), 0) as included_remaining,
          coalesce(sum(case when bucket_type = 'included' and status = 'active' and (expires_at is null or expires_at > now()) then reserved_amount else 0 end), 0) as included_reserved
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


async def _fetch_ledger_summary(conn: asyncpg.Connection, *, user_id: UUID) -> Dict[str, int]:
    if not await _table_exists(conn, "public.pricing_credit_ledger_events"):
        return {"ledger_count": 0, "credits_granted": 0, "credits_spent": 0}

    row = await conn.fetchrow(
        """
        select
          count(*) as ledger_count,
          coalesce(sum(case when credits_delta > 0 then credits_delta else 0 end), 0) as credits_granted,
          coalesce(sum(case when credits_delta < 0 then abs(credits_delta) else 0 end), 0) as credits_spent
        from pricing_credit_ledger_events
        where user_id = $1
        """,
        user_id,
    )
    if not row:
        return {"ledger_count": 0, "credits_granted": 0, "credits_spent": 0}
    return {
        "ledger_count": _safe_int(row.get("ledger_count"), 0),
        "credits_granted": _safe_int(row.get("credits_granted"), 0),
        "credits_spent": _safe_int(row.get("credits_spent"), 0),
    }


async def _has_active_non_free_subscription(conn: asyncpg.Connection, *, user_id: UUID) -> bool:
    if not await _table_exists(conn, "public.payment_plan_subscriptions"):
        return False

    row = await conn.fetchrow(
        """
        select 1
        from payment_plan_subscriptions
        where user_id = $1
          and coalesce(trim(plan_code), '') not in ('', 'free')
          and lower(coalesce(subscription_state, '')) = any($2::text[])
        limit 1
        """,
        user_id,
        sorted(_ACTIVE_PAID_SUBSCRIPTION_STATES),
    )
    return bool(row)


async def _insert_or_sync_free_plan_grant_lot(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    email: Optional[str],
    source: str,
    target_credits: int,
    existing_bill: Optional[Any],
    allow_full_grant_on_empty: bool,
) -> str:
    lot_summary = await _fetch_active_lot_summary(conn, user_id=user_id)
    if lot_summary["active_lot_count"] > 0:
        if (
            allow_full_grant_on_empty
            and lot_summary["included_granted"] > 0
            and lot_summary["included_remaining"] <= 0
            and lot_summary["included_reserved"] <= 0
        ):
            await conn.execute(
                """
                update pricing_credit_lots
                set granted_amount = greatest(granted_amount, $2::numeric),
                    remaining_amount = $2::numeric,
                    reserved_amount = 0,
                    plan_code_at_grant = coalesce(nullif(plan_code_at_grant, ''), 'free'),
                    metadata_json = case
                        when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
                        then metadata_json
                        else '{}'::jsonb
                      end || jsonb_build_object(
                        'source', $3::text,
                        'email', $4::text,
                        'bootstrap_kind', 'free_signup',
                        'bootstrap_applied', true,
                        'repaired_by', 'free_signup_bootstrap_service',
                        'repair_reason', 'active_free_lot_had_zero_remaining_without_usage_history'
                      ),
                    updated_at = now()
                where user_id = $1::uuid
                  and bucket_type = 'included'
                  and status = 'active'
                  and remaining_amount <= 0
                """,
                user_id,
                target_credits,
                source,
                email,
            )
            return "repaired_active_zero_lot"
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
    if remaining_amount <= 0 and balance_credits == 0 and reserved_credits == 0 and allow_full_grant_on_empty:
        remaining_amount = granted_amount
    remaining_amount = max(0, min(remaining_amount, granted_amount))

    reserved_amount = max(0, min(reserved_credits, remaining_amount))

    command = await conn.execute(
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
            'bootstrap_applied', true,
            'allow_full_grant_on_empty', $7::boolean
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
        bool(allow_full_grant_on_empty),
    )
    return "created" if command.endswith(" 1") else "preserved_race_existing"


async def _reconcile_free_billing_entitlement(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    email: Optional[str],
    source: str,
    target_credits: int,
    allow_full_grant_on_empty: bool,
) -> Any:
    lot_summary = await _fetch_active_lot_summary(conn, user_id=user_id)
    account = await _fetch_credit_account(conn, user_id=user_id)

    included_granted = lot_summary["included_granted"]
    included_remaining = lot_summary["included_remaining"]
    account_balance = _safe_int(account.get("balance_credits"), 0) if account else 0

    final_total = included_granted if included_granted > 0 else target_credits
    if final_total <= 0:
        final_total = target_credits

    final_remaining = included_remaining if included_remaining > 0 else account_balance
    if final_remaining <= 0:
        final_remaining = final_total if allow_full_grant_on_empty else 0
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
            "allow_full_grant_on_empty": bool(allow_full_grant_on_empty),
        },
    )


async def _direct_repair_free_entitlement_if_needed(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    email: Optional[str],
    source: str,
    target_credits: int,
    desired_remaining: int,
    reason: str,
) -> str:
    row = await conn.fetchrow(
        """
        select user_id, tier_code, plan_code, billing_mode, settlement_mode,
               included_credits_total, included_credits_remaining
        from billing_entitlements
        where user_id = $1
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by effective_from desc, updated_at desc
        limit 1
        """,
        user_id,
    )

    needs_repair = False
    if row is None:
        needs_repair = True
    else:
        needs_repair = (
            _safe_text(row.get("tier_code")) != "free"
            or _safe_text(row.get("plan_code")) != "free"
            or _safe_text(row.get("billing_mode")) != "free"
            or _safe_text(row.get("settlement_mode")) not in {"credits", "prepaid"}
            or _safe_int(row.get("included_credits_total"), 0) < int(target_credits)
            or _safe_int(row.get("included_credits_remaining"), 0) != int(desired_remaining)
        )

    if not needs_repair:
        return "not_needed"

    if row is None:
        await conn.execute(
            """
            insert into billing_entitlements(
              user_id, tier_code, plan_code, billing_mode, settlement_mode,
              included_credits_total, included_credits_remaining, overage_allowed,
              wallet_topup_allowed, hard_stop_on_insufficient_balance, source,
              metadata_json, updated_at
            )
            values(
              $1, 'free', 'free', 'free', 'credits',
              $2::numeric, $3::numeric, false,
              true, true, $4,
              jsonb_build_object(
                'source', $4::text,
                'email', $5::text,
                'bootstrap_kind', 'free_signup',
                'bootstrap_applied', true,
                'direct_repair', true,
                'repair_reason', $6::text
              ),
              now()
            )
            on conflict (user_id)
            do update set
              tier_code = 'free',
              plan_code = 'free',
              billing_mode = 'free',
              settlement_mode = 'credits',
              included_credits_total = greatest(billing_entitlements.included_credits_total, excluded.included_credits_total),
              included_credits_remaining = excluded.included_credits_remaining,
              overage_allowed = false,
              wallet_topup_allowed = true,
              hard_stop_on_insufficient_balance = true,
              source = excluded.source,
              metadata_json = excluded.metadata_json,
              updated_at = now()
            """,
            user_id,
            int(target_credits),
            int(desired_remaining),
            source,
            email,
            reason,
        )
        return "inserted_or_upserted"

    await conn.execute(
        """
        update billing_entitlements
        set tier_code = 'free',
            plan_code = 'free',
            billing_mode = 'free',
            settlement_mode = 'credits',
            included_credits_total = greatest(included_credits_total, $2::numeric),
            included_credits_remaining = $3::numeric,
            overage_allowed = false,
            wallet_topup_allowed = true,
            hard_stop_on_insufficient_balance = true,
            source = $4,
            metadata_json = case
                when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
                then metadata_json
                else '{}'::jsonb
              end || jsonb_build_object(
                'source', $4::text,
                'email', $5::text,
                'bootstrap_kind', 'free_signup',
                'bootstrap_applied', true,
                'direct_repair', true,
                'repair_reason', $6::text
              ),
            updated_at = now()
        where user_id = $1
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        """,
        user_id,
        int(target_credits),
        int(desired_remaining),
        source,
        email,
        reason,
    )
    return "updated"


async def _sync_credit_account_for_free_user(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    desired_balance: int,
    allow_full_grant_on_empty: bool,
) -> str:
    account = await _fetch_credit_account(conn, user_id=user_id)
    if account is None:
        await conn.execute(
            """
            insert into pricing_credit_accounts(
              user_id, balance_credits, reserved_credits, settlement_mode, updated_at
            )
            values($1, $2, 0, 'prepaid', now())
            on conflict (user_id) do nothing
            """,
            user_id,
            int(desired_balance),
        )
        return "created"

    balance_credits = _safe_int(account.get("balance_credits"), 0)
    reserved_credits = _safe_int(account.get("reserved_credits"), 0)
    billing_account_id = account.get("billing_account_id")
    settlement_mode = _safe_text(account.get("settlement_mode"))

    if billing_account_id is not None:
        return "preserved_existing_billing_account"

    if settlement_mode not in {"", "credits", "prepaid"}:
        return "preserved_non_prepaid_account"

    if balance_credits == 0 and reserved_credits == 0 and allow_full_grant_on_empty:
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
            int(desired_balance),
        )
        return "uplifted_zero_balance"

    if reserved_credits < 0:
        await conn.execute(
            """
            update pricing_credit_accounts
            set reserved_credits = 0,
                updated_at = now()
            where user_id = $1
              and reserved_credits < 0
            """,
            user_id,
        )
        return "normalized_negative_reserved"

    return "preserved_existing"


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
    ledger_summary = await _fetch_ledger_summary(conn, user_id=user_id)

    if await _has_active_non_free_subscription(conn, user_id=user_id):
        return {"bootstrapped": False, "reason": "existing_active_non_free_subscription"}
    if existing_ent and _safe_text(existing_ent.tier_code) not in {"", "free"}:
        return {"bootstrapped": False, "reason": "existing_non_free_user_entitlement"}
    if existing_bill and _safe_text(existing_bill.tier_code) not in {"", "free"}:
        return {"bootstrapped": False, "reason": "existing_non_free_billing_entitlement"}
    if existing_bill and _safe_text(existing_bill.billing_mode) not in {"", "free"}:
        return {"bootstrapped": False, "reason": "existing_non_free_billing_mode"}

    if core_tier not in {"", "free"} and not existing_ent and not existing_bill:
        return {"bootstrapped": False, "reason": f"core_tier_not_free:{core_tier}"}

    has_spend_history = ledger_summary["credits_spent"] > 0 or ledger_summary["ledger_count"] > 0
    allow_full_grant_on_empty = not has_spend_history

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

    desired_total = max(existing_total, target_credits)
    desired_remaining = existing_remaining if existing_remaining > 0 else account_balance
    if desired_remaining <= 0:
        desired_remaining = target_credits if allow_full_grant_on_empty else 0
    desired_remaining = max(0, min(desired_remaining, desired_total))

    if existing_bill is None:
        await _BILLING_ENTITLEMENTS_REPO.upsert_billing_entitlement(
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
                "allow_full_grant_on_empty": allow_full_grant_on_empty,
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
            and existing_settlement_mode in {"", "credits", "prepaid"}
        ):
            await _BILLING_ENTITLEMENTS_REPO.upsert_billing_entitlement(
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
                    "allow_full_grant_on_empty": allow_full_grant_on_empty,
                },
            )
            billing_entitlement_action = "uplifted_zero_balance"
        else:
            billing_entitlement_action = "preserved_existing"

    account_action = await _sync_credit_account_for_free_user(
        conn,
        user_id=user_id,
        desired_balance=desired_remaining,
        allow_full_grant_on_empty=allow_full_grant_on_empty,
    )

    current_bill = await _BILLING_ENTITLEMENTS_REPO.get_active_by_user_id(conn, user_id=user_id)
    lot_action = await _insert_or_sync_free_plan_grant_lot(
        conn,
        user_id=user_id,
        email=effective_email,
        source=source,
        target_credits=target_credits,
        existing_bill=current_bill,
        allow_full_grant_on_empty=allow_full_grant_on_empty,
    )

    reconciled_bill = await _reconcile_free_billing_entitlement(
        conn,
        user_id=user_id,
        email=effective_email,
        source=source,
        target_credits=target_credits,
        allow_full_grant_on_empty=allow_full_grant_on_empty,
    )

    lot_summary = await _fetch_active_lot_summary(conn, user_id=user_id)
    final_remaining = lot_summary["included_remaining"]
    if final_remaining <= 0 and allow_full_grant_on_empty:
        final_remaining = target_credits
    final_remaining = max(0, min(final_remaining, max(target_credits, lot_summary["included_granted"])))

    direct_repair_action = await _direct_repair_free_entitlement_if_needed(
        conn,
        user_id=user_id,
        email=effective_email,
        source=source,
        target_credits=target_credits,
        desired_remaining=final_remaining,
        reason="final_free_signup_contract_guard",
    )

    # Re-read after the direct guard so the route response reflects DB truth.
    reconciled_bill = await _BILLING_ENTITLEMENTS_REPO.get_active_by_user_id(conn, user_id=user_id) or reconciled_bill
    lot_summary = await _fetch_active_lot_summary(conn, user_id=user_id)
    account = await _fetch_credit_account(conn, user_id=user_id)
    account_balance_final = _safe_int(account.get("balance_credits"), 0) if account else 0

    return {
        "bootstrapped": True,
        "user_id": str(user_id),
        "tier_code": ent.tier_code,
        "plan_code": reconciled_bill.plan_code,
        "included_credits_total": _safe_int(reconciled_bill.included_credits_total, target_credits),
        "included_credits_remaining": _safe_int(reconciled_bill.included_credits_remaining, final_remaining),
        "target_credits": target_credits,
        "granted_balance_credits": max(lot_summary["included_remaining"], account_balance_final),
        "core_tier_action": core_tier_action,
        "billing_entitlement_action": billing_entitlement_action,
        "account_action": account_action,
        "lot_action": lot_action,
        "direct_repair_action": direct_repair_action,
        "billing_entitlement_reconciled": True,
        "active_lot_count": lot_summary["active_lot_count"],
        "included_granted": lot_summary["included_granted"],
        "included_remaining": lot_summary["included_remaining"],
        "included_reserved": lot_summary["included_reserved"],
        "ledger_count": ledger_summary["ledger_count"],
        "credits_spent": ledger_summary["credits_spent"],
        "allow_full_grant_on_empty": allow_full_grant_on_empty,
    }
