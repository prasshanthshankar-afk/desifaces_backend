from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID


def as_dict_loose(x: Any) -> Dict[str, Any]:
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


async def table_exists(conn, table_name: str) -> bool:
    row = await conn.fetchrow(
        """
        select exists (
          select 1
          from information_schema.tables
          where table_schema = 'public'
            and table_name = $1
        ) as present
        """,
        table_name,
    )
    return bool(row["present"]) if row else False


async def upsert_apple_transaction_audit(
    conn,
    *,
    user_id: UUID,
    apple_product_id: str,
    product_type: str,
    transaction_id: str,
    original_transaction_id: Optional[str],
    app_account_token: Optional[UUID],
    environment: str,
    currency: str,
    country_code: str,
    storefront: Optional[str],
    storefront_id: Optional[str],
    purchase_date: Optional[datetime],
    expires_date: Optional[datetime],
    ownership_type: Optional[str],
    transaction_reason: Optional[str],
    raw_signed_transaction: str,
    raw_signed_renewal: Optional[str],
    raw_decoded_json: Dict[str, Any],
) -> bool:
    """Insert/update Apple transaction audit idempotently.

    Xcode-local StoreKit can return transaction_id='0', and the frontend may
    deliver the purchase callback more than once. The previous read-then-insert
    implementation was race-prone and raised UniqueViolationError on duplicate
    callbacks. Keep the audit write idempotent so entitlement confirmation can
    safely proceed/retry. Production App Store/TestFlight transaction ids remain
    unique and are preserved as-is.
    """
    if not await table_exists(conn, "apple_iap_transactions"):
        raise RuntimeError("apple_iap_tables_missing")

    txid = str(transaction_id or "").strip()
    if not txid:
        # Should be rare, but avoid inserting an empty transaction id. This is
        # deterministic for repeated callbacks with the same signed payload.
        import hashlib

        digest_src = "|".join(
            [
                str(user_id),
                str(apple_product_id or ""),
                str(original_transaction_id or ""),
                str(raw_signed_transaction or ""),
            ]
        )
        txid = "local-missing-" + hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:32]

    row = await conn.fetchrow(
        """
        insert into public.apple_iap_transactions (
          user_id, apple_product_id, product_type, transaction_id, original_transaction_id,
          app_account_token, environment, currency, country_code, storefront, storefront_id,
          purchase_date, expires_date, ownership_type, transaction_reason,
          raw_signed_transaction, raw_signed_renewal, raw_decoded_json, processed_status, created_at, updated_at
        ) values (
          $1, $2, $3, $4, $5,
          $6, $7, $8, $9, $10, $11,
          $12, $13, $14, $15,
          $16, $17, $18::jsonb, 'processed', now(), now()
        )
        on conflict (transaction_id)
        do update set
          user_id = excluded.user_id,
          apple_product_id = excluded.apple_product_id,
          product_type = excluded.product_type,
          original_transaction_id = coalesce(excluded.original_transaction_id, public.apple_iap_transactions.original_transaction_id),
          app_account_token = coalesce(excluded.app_account_token, public.apple_iap_transactions.app_account_token),
          environment = coalesce(excluded.environment, public.apple_iap_transactions.environment),
          currency = coalesce(excluded.currency, public.apple_iap_transactions.currency),
          country_code = coalesce(excluded.country_code, public.apple_iap_transactions.country_code),
          storefront = coalesce(excluded.storefront, public.apple_iap_transactions.storefront),
          storefront_id = coalesce(excluded.storefront_id, public.apple_iap_transactions.storefront_id),
          purchase_date = coalesce(excluded.purchase_date, public.apple_iap_transactions.purchase_date),
          expires_date = coalesce(excluded.expires_date, public.apple_iap_transactions.expires_date),
          ownership_type = coalesce(excluded.ownership_type, public.apple_iap_transactions.ownership_type),
          transaction_reason = coalesce(excluded.transaction_reason, public.apple_iap_transactions.transaction_reason),
          raw_signed_transaction = coalesce(excluded.raw_signed_transaction, public.apple_iap_transactions.raw_signed_transaction),
          raw_signed_renewal = coalesce(excluded.raw_signed_renewal, public.apple_iap_transactions.raw_signed_renewal),
          raw_decoded_json = coalesce(public.apple_iap_transactions.raw_decoded_json, '{}'::jsonb) || excluded.raw_decoded_json,
          processed_status = 'processed',
          updated_at = now()
        returning (xmax = 0) as inserted
        """,
        user_id,
        apple_product_id,
        product_type,
        txid,
        original_transaction_id,
        app_account_token,
        environment,
        currency,
        country_code,
        storefront,
        storefront_id,
        purchase_date,
        expires_date,
        ownership_type,
        transaction_reason,
        raw_signed_transaction,
        raw_signed_renewal,
        json.dumps(raw_decoded_json, default=str),
    )
    return bool(row and row.get("inserted"))


async def record_apple_notification_event(
    conn,
    *,
    notification_uuid: str,
    notification_type: str,
    subtype: Optional[str],
    environment: str,
    signed_payload: str,
    decoded_payload_json: Dict[str, Any],
    transaction_id: Optional[str],
    original_transaction_id: Optional[str],
    app_account_token: Optional[UUID],
) -> bool:
    if not await table_exists(conn, "apple_iap_notification_events"):
        raise RuntimeError("apple_iap_tables_missing")
    existing = await conn.fetchrow(
        "select notification_uuid from public.apple_iap_notification_events where notification_uuid = $1 limit 1",
        notification_uuid,
    )
    if existing:
        return False
    await conn.execute(
        """
        insert into public.apple_iap_notification_events (
          notification_uuid, notification_type, subtype, environment, signed_payload,
          decoded_payload_json, transaction_id, original_transaction_id, app_account_token,
          processing_status, created_at, updated_at
        ) values (
          $1, $2, $3, $4, $5,
          $6::jsonb, $7, $8, $9,
          'processed', now(), now()
        )
        """,
        notification_uuid,
        notification_type,
        subtype,
        environment,
        signed_payload,
        json.dumps(decoded_payload_json, default=str),
        transaction_id,
        original_transaction_id,
        app_account_token,
    )
    return True


async def resolve_apple_product_mapping(
    conn,
    *,
    apple_product_id: str,
    expected_product_type: str,
    currency: str,
    country_code: str,
) -> Dict[str, Any]:
    if not await table_exists(conn, "apple_iap_product_mappings"):
        raise RuntimeError("apple_iap_mappings_missing")
    row = await conn.fetchrow(
        """
        select apple_product_id, product_type, credits, currency, country_code,
               internal_pack_code, internal_plan_code, is_active, metadata_json
        from public.apple_iap_product_mappings
        where apple_product_id = $1
          and product_type = $2
          and is_active = true
          and currency = $3
          and country_code = $4
        limit 1
        """,
        apple_product_id,
        expected_product_type,
        currency,
        country_code,
    )
    if not row:
        row = await conn.fetchrow(
            """
            select apple_product_id, product_type, credits, currency, country_code,
                   internal_pack_code, internal_plan_code, is_active, metadata_json
            from public.apple_iap_product_mappings
            where apple_product_id = $1
              and product_type = $2
              and is_active = true
              and currency = $3
              and country_code = ''
            limit 1
            """,
            apple_product_id,
            expected_product_type,
            currency,
        )
    if not row:
        raise LookupError("apple_product_not_mapped")
    return dict(row)


async def fetch_plan_profile(conn, *, internal_plan_code: str) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        select
          p.plan_code,
          p.tier_code,
          p.interval_code,
          p.metadata_json,
          t.monthly_grant_credits
        from public.pricing_plan_prices p
        join public.pricing_tiers t
          on t.code = p.tier_code
        where lower(p.plan_code) = lower($1)
          and p.is_active = true
        order by p.created_at desc
        limit 1
        """,
        internal_plan_code,
    )
    if not row:
        raise LookupError("apple_plan_profile_missing")

    out = dict(row)
    interval_code = str(out.get("interval_code") or "monthly").strip().lower()
    try:
        base_grant = int(out.get("monthly_grant_credits") or 0)
    except Exception:
        base_grant = 0

    md = as_dict_loose(out.get("metadata_json"))
    explicit_grant = None
    for key in ("included_credits_total", "included_credits", "credits_total", "grant_credits"):
        raw = md.get(key)
        if raw is None or raw == "":
            continue
        try:
            explicit_grant = int(raw)
            break
        except Exception:
            continue

    # Keep the legacy key name because apple_iap_service consumes it, but make
    # its value represent the entitlement grant for this billing interval. A
    # yearly Pro/Business Apple subscription should refresh annual credits, not
    # the monthly tier grant.
    if explicit_grant is not None:
        out["monthly_grant_credits"] = explicit_grant
    elif interval_code == "yearly":
        out["monthly_grant_credits"] = base_grant * 12
    else:
        out["monthly_grant_credits"] = base_grant

    return out


async def fetch_credit_pack_profile(conn, *, internal_pack_code: str) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        select code, name, credits, currency, country_code, price_money, metadata_json
        from public.pricing_credit_packs
        where code = $1
          and is_active = true
        limit 1
        """,
        internal_pack_code,
    )
    if not row:
        raise LookupError("apple_credit_pack_missing")
    return dict(row)


async def upsert_apple_subscription_row(
    conn,
    *,
    user_id: UUID,
    app_account_token: Optional[UUID],
    original_transaction_id: str,
    apple_product_id: str,
    internal_plan_code: str,
    subscription_state: str,
    entitlement_state: str,
    current_period_start: Optional[datetime],
    current_period_end: Optional[datetime],
    cancel_at_period_end: bool,
    canceled_at: Optional[datetime],
    trial_start: Optional[datetime],
    trial_end: Optional[datetime],
    metadata_json: Dict[str, Any],
) -> None:
    await conn.execute(
        """
        insert into public.payment_plan_subscriptions (
          user_id, gateway_provider, gateway_customer_id, gateway_subscription_id, gateway_price_id,
          plan_code, subscription_state, current_period_start, current_period_end, cancel_at_period_end,
          canceled_at, latest_invoice_id, latest_invoice_status, entitlement_state, trial_start, trial_end, metadata_json
        ) values (
          $1, 'apple_iap', $2, $3, $4,
          $5, $6, $7, $8, $9,
          $10, null, null, $11, $12, $13, $14::jsonb
        )
        on conflict (gateway_subscription_id)
        do update set
          user_id = excluded.user_id,
          gateway_provider = 'apple_iap',
          gateway_customer_id = excluded.gateway_customer_id,
          gateway_price_id = excluded.gateway_price_id,
          plan_code = excluded.plan_code,
          subscription_state = excluded.subscription_state,
          current_period_start = excluded.current_period_start,
          current_period_end = excluded.current_period_end,
          cancel_at_period_end = excluded.cancel_at_period_end,
          canceled_at = coalesce(excluded.canceled_at, public.payment_plan_subscriptions.canceled_at),
          entitlement_state = excluded.entitlement_state,
          trial_start = coalesce(public.payment_plan_subscriptions.trial_start, excluded.trial_start),
          trial_end = excluded.trial_end,
          metadata_json = coalesce(public.payment_plan_subscriptions.metadata_json, '{}'::jsonb) || excluded.metadata_json,
          updated_at = now()
        """,
        user_id,
        str(app_account_token) if app_account_token else None,
        original_transaction_id,
        apple_product_id,
        internal_plan_code,
        subscription_state,
        current_period_start,
        current_period_end,
        cancel_at_period_end,
        canceled_at,
        entitlement_state,
        trial_start,
        trial_end,
        json.dumps(metadata_json, default=str),
    )


async def fetch_open_entitlement_for_update(conn, *, user_id: UUID):
    """Lock the user's entitlement row.

    Production deployments currently enforce one billing_entitlements row per
    user via billing_entitlements_user_id_key. Older Apple code treated this as
    a historical table and tried to close the active row + insert a new row,
    which fails for upgrades with UniqueViolationError. Read/lock by user_id so
    confirm/retry/change flows update the canonical row in place.
    """
    return await conn.fetchrow(
        """
        select *
        from public.billing_entitlements
        where user_id = $1
        for update
        """,
        user_id,
    )


async def apply_subscription_entitlement(
    conn,
    *,
    user_id: UUID,
    tier_code: str,
    internal_plan_code: str,
    cycle_key: str,
    monthly_grant_credits: int,
    current_period_start: Optional[datetime],
    source: str,
    metadata_json: Dict[str, Any],
) -> None:
    """Apply an Apple subscription entitlement idempotently.

    billing_entitlements is unique by user_id in the current schema. Apple
    subscription confirmations must therefore update/upsert the user's single
    canonical entitlement row, not expire one row and insert another.
    """
    grant_credits = max(int(monthly_grant_credits or 0), 0)
    metadata = dict(metadata_json or {})
    metadata["apple_iap_cycle_key"] = cycle_key
    metadata["provider"] = metadata.get("provider") or "apple_iap"

    open_row = await fetch_open_entitlement_for_update(conn, user_id=user_id)
    if open_row:
        open_md = as_dict_loose(open_row.get("metadata_json"))
        same_plan = str(open_row.get("plan_code") or "").strip().lower() == str(internal_plan_code).strip().lower()
        same_cycle = str(open_md.get("apple_iap_cycle_key") or "").strip() == cycle_key

        if same_plan and same_cycle:
            # Idempotent retry of the same Apple transaction/cycle. Do not grant
            # credits again; only merge metadata and keep the row active/current.
            await conn.execute(
                """
                update public.billing_entitlements
                set tier_code = $2,
                    plan_code = $3,
                    billing_mode = 'subscription',
                    settlement_mode = 'credits',
                    effective_to = null,
                    source = $4,
                    metadata_json = (
                      case
                        when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
                          then coalesce(metadata_json, '{}'::jsonb)
                        else '{}'::jsonb
                      end
                    ) || $5::jsonb,
                    updated_at = now()
                where user_id = $1
                """,
                user_id,
                tier_code,
                internal_plan_code,
                source,
                json.dumps(metadata, default=str),
            )
            return

        # New Apple billing cycle or a plan change. Refresh included credits to
        # the target plan grant once. This covers free->paid, monthly->yearly,
        # Stripe->Apple, Apple monthly->Apple yearly, and notification renewals.
        await conn.execute(
            """
            update public.billing_entitlements
            set tier_code = $2,
                plan_code = $3,
                billing_mode = 'subscription',
                settlement_mode = 'credits',
                included_credits_total = $4,
                included_credits_remaining = $4,
                overage_allowed = false,
                wallet_topup_allowed = true,
                hard_stop_on_insufficient_balance = true,
                effective_from = coalesce($5, now()),
                effective_to = null,
                source = $6,
                metadata_json = (
                  case
                    when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
                      then coalesce(metadata_json, '{}'::jsonb)
                    else '{}'::jsonb
                  end
                ) || $7::jsonb,
                updated_at = now()
            where user_id = $1
            """,
            user_id,
            tier_code,
            internal_plan_code,
            grant_credits,
            current_period_start,
            source,
            json.dumps(metadata, default=str),
        )
        return

    # Brand-new user/path with no entitlement row yet.
    await conn.execute(
        """
        insert into public.billing_entitlements (
          user_id, tier_code, plan_code, billing_mode, settlement_mode,
          included_credits_total, included_credits_remaining,
          overage_allowed, wallet_topup_allowed, hard_stop_on_insufficient_balance,
          effective_from, effective_to, source, metadata_json, created_at, updated_at
        ) values (
          $1, $2, $3, 'subscription', 'credits',
          $4, $4,
          false, true, true,
          coalesce($5, now()), null, $6, $7::jsonb, now(), now()
        )
        on conflict (user_id)
        do update set
          tier_code = excluded.tier_code,
          plan_code = excluded.plan_code,
          billing_mode = excluded.billing_mode,
          settlement_mode = excluded.settlement_mode,
          included_credits_total = excluded.included_credits_total,
          included_credits_remaining = excluded.included_credits_remaining,
          overage_allowed = excluded.overage_allowed,
          wallet_topup_allowed = excluded.wallet_topup_allowed,
          hard_stop_on_insufficient_balance = excluded.hard_stop_on_insufficient_balance,
          effective_from = excluded.effective_from,
          effective_to = null,
          source = excluded.source,
          metadata_json = (
            case
              when jsonb_typeof(coalesce(public.billing_entitlements.metadata_json, '{}'::jsonb)) = 'object'
                then coalesce(public.billing_entitlements.metadata_json, '{}'::jsonb)
              else '{}'::jsonb
            end
          ) || excluded.metadata_json,
          updated_at = now()
        """,
        user_id,
        tier_code,
        internal_plan_code,
        grant_credits,
        current_period_start,
        source,
        json.dumps(metadata, default=str),
    )


async def revert_user_to_free_entitlement(
    conn,
    *,
    user_id: UUID,
    source: str,
    metadata_json: Dict[str, Any],
) -> None:
    """Move the user's canonical entitlement back to Free.

    Keep this as an upsert/update because billing_entitlements is unique by
    user_id in production.
    """
    free_row = await conn.fetchrow(
        "select code, monthly_grant_credits from public.pricing_tiers where code = 'free' limit 1"
    )
    free_credits = int(free_row.get("monthly_grant_credits") or 0) if free_row else 0
    metadata = dict(metadata_json or {})
    metadata["provider"] = metadata.get("provider") or "apple_iap"
    metadata["reverted_to_free"] = True

    await conn.execute(
        """
        insert into public.billing_entitlements (
          user_id, tier_code, plan_code, billing_mode, settlement_mode,
          included_credits_total, included_credits_remaining,
          overage_allowed, wallet_topup_allowed, hard_stop_on_insufficient_balance,
          effective_from, effective_to, source, metadata_json, created_at, updated_at
        ) values (
          $1, 'free', 'free', 'free', 'credits',
          $2, $2,
          false, true, true,
          now(), null, $3, $4::jsonb, now(), now()
        )
        on conflict (user_id)
        do update set
          tier_code = excluded.tier_code,
          plan_code = excluded.plan_code,
          billing_mode = excluded.billing_mode,
          settlement_mode = excluded.settlement_mode,
          included_credits_total = excluded.included_credits_total,
          included_credits_remaining = excluded.included_credits_remaining,
          overage_allowed = excluded.overage_allowed,
          wallet_topup_allowed = excluded.wallet_topup_allowed,
          hard_stop_on_insufficient_balance = excluded.hard_stop_on_insufficient_balance,
          effective_from = excluded.effective_from,
          effective_to = null,
          source = excluded.source,
          metadata_json = (
            case
              when jsonb_typeof(coalesce(public.billing_entitlements.metadata_json, '{}'::jsonb)) = 'object'
                then coalesce(public.billing_entitlements.metadata_json, '{}'::jsonb)
              else '{}'::jsonb
            end
          ) || excluded.metadata_json,
          updated_at = now()
        """,
        user_id,
        free_credits,
        source,
        json.dumps(metadata, default=str),
    )


async def best_effort_grant_apple_credits(
    conn,
    *,
    user_id: UUID,
    internal_pack_code: str,
    pack_profile: Dict[str, Any],
    transaction_id: str,
    original_transaction_id: Optional[str],
    apple_product_id: str,
    currency: str,
    country_code: str,
    storefront: Optional[str],
) -> Dict[str, Any]:
    credits = int(pack_profile.get("credits") or 0)
    amount_minor = int(Decimal(str(pack_profile.get("price_money") or 0)) * Decimal("100"))
    wallet_order_id = None
    idempotency_key = f"apple_iap:{transaction_id}"

    if await table_exists(conn, "payment_wallet_orders"):
        existing = await conn.fetchrow(
            """
            select id, payment_state, fulfillment_state
            from public.payment_wallet_orders
            where user_id = $1
              and idempotency_key = $2
            limit 1
            """,
            user_id,
            idempotency_key,
        )
        if existing:
            wallet_order_id = str(existing["id"])
        else:
            row = await conn.fetchrow(
                """
                insert into public.payment_wallet_orders (
                  user_id, order_type, currency, amount_minor, credits_to_grant,
                  gateway_provider, payment_state, fulfillment_state, idempotency_key,
                  metadata_json, created_at, updated_at
                ) values (
                  $1, 'topup', $2, $3, $4,
                  'apple_iap', 'succeeded', 'pending', $5,
                  $6::jsonb, now(), now()
                ) returning id
                """,
                user_id,
                str(pack_profile.get("currency") or currency or "USD"),
                amount_minor,
                Decimal(str(credits)),
                idempotency_key,
                json.dumps({
                    "provider": "apple_iap",
                    "apple_product_id": apple_product_id,
                    "internal_pack_code": internal_pack_code,
                    "transaction_id": transaction_id,
                    "original_transaction_id": original_transaction_id,
                    "country_code": country_code,
                    "storefront": storefront,
                    "fulfillment_source": "wallet_order",
                    "fulfillment_status": "pending",
                }, default=str),
            )
            wallet_order_id = str(row["id"]) if row else None

    # IMPORTANT:
    # Do NOT write directly to pricing_credit_accounts here.
    #
    # Stripe top-ups follow the canonical wallet-order -> fulfillment path, and
    # direct balance mutation here bypasses the same business rules/guardrails.
    # In production this Apple path should be fulfilled by the same internal
    # wallet grant flow that processes succeeded top-up orders.
    return {
        "wallet_order_id": wallet_order_id,
        "granted_credits": credits,
        "fulfillment_state": "pending",
    }


async def find_subscription_user_id(conn, *, original_transaction_id: str):
    row = await conn.fetchrow(
        "select user_id from public.payment_plan_subscriptions where gateway_subscription_id = $1 limit 1",
        original_transaction_id,
    )
    return row.get("user_id") if row else None
