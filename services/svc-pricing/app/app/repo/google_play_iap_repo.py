from __future__ import annotations

import hashlib
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


def sha256_hex(value: str) -> str:
    raw = str(value or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


async def resolve_google_product_mapping(
    conn,
    *,
    google_product_id: str,
    expected_product_type: str,
    currency: str,
    country_code: str,
    base_plan_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not await table_exists(conn, "google_play_iap_product_mappings"):
        raise RuntimeError("google_play_iap_mappings_missing")

    product_id = str(google_product_id or "").strip()
    product_type = str(expected_product_type or "").strip().lower()
    ccy = str(currency or "").strip().upper()
    cc = str(country_code or "").strip().upper()
    base_plan = str(base_plan_id or "").strip()

    row = await conn.fetchrow(
        """
        select google_product_id, base_plan_id, product_type, credits, currency, country_code,
               internal_pack_code, internal_plan_code, is_active, metadata_json
        from public.google_play_iap_product_mappings
        where google_product_id = $1
          and product_type = $2
          and base_plan_id = $3
          and is_active = true
          and currency = $4
          and country_code = $5
        limit 1
        """,
        product_id,
        product_type,
        base_plan,
        ccy,
        cc,
    )
    if not row:
        row = await conn.fetchrow(
            """
            select google_product_id, base_plan_id, product_type, credits, currency, country_code,
                   internal_pack_code, internal_plan_code, is_active, metadata_json
            from public.google_play_iap_product_mappings
            where google_product_id = $1
              and product_type = $2
              and base_plan_id = $3
              and is_active = true
              and currency = $4
              and country_code = ''
            limit 1
            """,
            product_id,
            product_type,
            base_plan,
            ccy,
        )
    if not row and product_type == "subscription" and base_plan:
        # Last resort: product id is globally unique in our launch catalog, but
        # keep base_plan_id in the table/audit for future offers. This makes the
        # backend tolerant of Android clients that fail to pass basePlanId.
        row = await conn.fetchrow(
            """
            select google_product_id, base_plan_id, product_type, credits, currency, country_code,
                   internal_pack_code, internal_plan_code, is_active, metadata_json
            from public.google_play_iap_product_mappings
            where google_product_id = $1
              and product_type = $2
              and is_active = true
              and currency = $3
              and coalesce(country_code, '') in ($4, '')
            order by
              case when coalesce(country_code, '') = $4 then 0 else 1 end,
              updated_at desc,
              created_at desc
            limit 1
            """,
            product_id,
            product_type,
            ccy,
            cc,
        )
    if not row:
        raise LookupError("google_play_product_not_mapped")
    return dict(row)


async def upsert_google_purchase_audit(
    conn,
    *,
    user_id: UUID,
    google_product_id: str,
    base_plan_id: str,
    product_type: str,
    package_name: str,
    purchase_token_hash: str,
    order_id: Optional[str],
    linked_purchase_token_hash: Optional[str],
    purchase_state: Optional[str],
    acknowledgement_state: Optional[str],
    consumption_state: Optional[str],
    subscription_state: Optional[str],
    internal_pack_code: Optional[str],
    internal_plan_code: Optional[str],
    raw_purchase_json: Dict[str, Any],
    fulfillment_state: str = "pending",
) -> bool:
    if not await table_exists(conn, "google_play_iap_purchases"):
        raise RuntimeError("google_play_iap_tables_missing")

    row = await conn.fetchrow(
        """
        insert into public.google_play_iap_purchases (
          user_id,
          google_product_id,
          base_plan_id,
          product_type,
          package_name,
          purchase_token_hash,
          order_id,
          linked_purchase_token_hash,
          purchase_state,
          acknowledgement_state,
          consumption_state,
          subscription_state,
          internal_pack_code,
          internal_plan_code,
          raw_purchase_json,
          processed_status,
          fulfillment_state,
          created_at,
          updated_at
        ) values (
          $1, $2, $3, $4, $5, $6,
          $7, $8, $9, $10, $11, $12,
          $13, $14, $15::jsonb, 'processed', $16, now(), now()
        )
        on conflict (package_name, google_product_id, purchase_token_hash)
        do update set
          user_id = excluded.user_id,
          base_plan_id = coalesce(nullif(excluded.base_plan_id, ''), public.google_play_iap_purchases.base_plan_id),
          order_id = coalesce(excluded.order_id, public.google_play_iap_purchases.order_id),
          linked_purchase_token_hash = coalesce(excluded.linked_purchase_token_hash, public.google_play_iap_purchases.linked_purchase_token_hash),
          purchase_state = coalesce(excluded.purchase_state, public.google_play_iap_purchases.purchase_state),
          acknowledgement_state = coalesce(excluded.acknowledgement_state, public.google_play_iap_purchases.acknowledgement_state),
          consumption_state = coalesce(excluded.consumption_state, public.google_play_iap_purchases.consumption_state),
          subscription_state = coalesce(excluded.subscription_state, public.google_play_iap_purchases.subscription_state),
          internal_pack_code = coalesce(excluded.internal_pack_code, public.google_play_iap_purchases.internal_pack_code),
          internal_plan_code = coalesce(excluded.internal_plan_code, public.google_play_iap_purchases.internal_plan_code),
          raw_purchase_json = coalesce(public.google_play_iap_purchases.raw_purchase_json, '{}'::jsonb) || excluded.raw_purchase_json,
          processed_status = 'processed',
          fulfillment_state = case
            when public.google_play_iap_purchases.fulfillment_state in ('granted', 'fulfilled') then public.google_play_iap_purchases.fulfillment_state
            else excluded.fulfillment_state
          end,
          updated_at = now()
        returning (xmax = 0) as inserted
        """,
        user_id,
        google_product_id,
        base_plan_id or "",
        product_type,
        package_name,
        purchase_token_hash,
        order_id,
        linked_purchase_token_hash,
        purchase_state,
        acknowledgement_state,
        consumption_state,
        subscription_state,
        internal_pack_code,
        internal_plan_code,
        json.dumps(raw_purchase_json, default=str),
        fulfillment_state,
    )
    return bool(row and row.get("inserted"))


async def mark_google_purchase_fulfilled(
    conn,
    *,
    package_name: str,
    google_product_id: str,
    purchase_token_hash: str,
    fulfillment_state: str,
) -> None:
    if not await table_exists(conn, "google_play_iap_purchases"):
        return
    await conn.execute(
        """
        update public.google_play_iap_purchases
        set fulfillment_state = $4,
            updated_at = now()
        where package_name = $1
          and google_product_id = $2
          and purchase_token_hash = $3
        """,
        package_name,
        google_product_id,
        purchase_token_hash,
        fulfillment_state,
    )


async def record_google_notification_event(
    conn,
    *,
    message_id: str,
    notification_type: str,
    package_name: Optional[str],
    google_product_id: Optional[str],
    purchase_token_hash: Optional[str],
    decoded_payload_json: Dict[str, Any],
    processing_status: str = "processed",
) -> bool:
    if not await table_exists(conn, "google_play_iap_notification_events"):
        raise RuntimeError("google_play_iap_tables_missing")

    row = await conn.fetchrow(
        """
        insert into public.google_play_iap_notification_events (
          message_id,
          notification_type,
          package_name,
          google_product_id,
          purchase_token_hash,
          decoded_payload_json,
          processing_status,
          created_at,
          updated_at
        ) values (
          $1, $2, $3, $4, $5, $6::jsonb, $7, now(), now()
        )
        on conflict (message_id)
        do update set
          processing_status = public.google_play_iap_notification_events.processing_status,
          updated_at = public.google_play_iap_notification_events.updated_at
        returning (xmax = 0) as inserted
        """,
        message_id,
        notification_type,
        package_name,
        google_product_id,
        purchase_token_hash,
        json.dumps(decoded_payload_json, default=str),
        processing_status,
    )
    return bool(row and row.get("inserted"))


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
        raise LookupError("google_play_plan_profile_missing")

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
        raise LookupError("google_play_credit_pack_missing")
    return dict(row)


async def upsert_google_subscription_row(
    conn,
    *,
    user_id: UUID,
    purchase_token_hash: str,
    linked_purchase_token_hash: Optional[str],
    google_product_id: str,
    base_plan_id: str,
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
    gateway_subscription_id = purchase_token_hash
    await conn.execute(
        """
        insert into public.payment_plan_subscriptions (
          user_id, gateway_provider, gateway_customer_id, gateway_subscription_id, gateway_price_id,
          plan_code, subscription_state, current_period_start, current_period_end, cancel_at_period_end,
          canceled_at, latest_invoice_id, latest_invoice_status, entitlement_state, trial_start, trial_end, metadata_json
        ) values (
          $1, 'google_play', $2, $3, $4,
          $5, $6, $7, $8, $9,
          $10, null, null, $11, $12, $13, $14::jsonb
        )
        on conflict (gateway_subscription_id)
        do update set
          user_id = excluded.user_id,
          gateway_provider = 'google_play',
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
        str(user_id),
        gateway_subscription_id,
        f"{google_product_id}:{base_plan_id}" if base_plan_id else google_product_id,
        internal_plan_code,
        subscription_state,
        current_period_start,
        current_period_end,
        cancel_at_period_end,
        canceled_at,
        entitlement_state,
        trial_start,
        trial_end,
        json.dumps(
            {
                **(metadata_json or {}),
                "purchase_token_hash": purchase_token_hash,
                "linked_purchase_token_hash": linked_purchase_token_hash,
            },
            default=str,
        ),
    )


async def fetch_open_entitlement_for_update(conn, *, user_id: UUID):
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
    grant_credits: int,
    current_period_start: Optional[datetime],
    source: str,
    metadata_json: Dict[str, Any],
) -> None:
    grant = max(int(grant_credits or 0), 0)
    metadata = dict(metadata_json or {})
    metadata["google_play_cycle_key"] = cycle_key
    metadata["provider"] = metadata.get("provider") or "google_play"

    open_row = await fetch_open_entitlement_for_update(conn, user_id=user_id)
    if open_row:
        open_md = as_dict_loose(open_row.get("metadata_json"))
        same_plan = str(open_row.get("plan_code") or "").strip().lower() == str(internal_plan_code).strip().lower()
        same_cycle = str(open_md.get("google_play_cycle_key") or "").strip() == cycle_key

        if same_plan and same_cycle:
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
            grant,
            current_period_start,
            source,
            json.dumps(metadata, default=str),
        )
        return

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
        grant,
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
    free_row = await conn.fetchrow(
        "select code, monthly_grant_credits from public.pricing_tiers where code = 'free' limit 1"
    )
    free_credits = int(free_row.get("monthly_grant_credits") or 0) if free_row else 0
    metadata = dict(metadata_json or {})
    metadata["provider"] = metadata.get("provider") or "google_play"
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


async def best_effort_grant_google_credits(
    conn,
    *,
    user_id: UUID,
    internal_pack_code: str,
    pack_profile: Dict[str, Any],
    purchase_token_hash: str,
    order_id: Optional[str],
    google_product_id: str,
    package_name: str,
    currency: str,
    country_code: str,
) -> Dict[str, Any]:
    credits = int(pack_profile.get("credits") or 0)
    amount_minor = int(Decimal(str(pack_profile.get("price_money") or 0)) * Decimal("100"))
    wallet_order_id = None
    idempotency_key = f"google_play:{purchase_token_hash}"

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
                  'google_play', 'succeeded', 'pending', $5,
                  $6::jsonb, now(), now()
                ) returning id
                """,
                user_id,
                str(pack_profile.get("currency") or currency or "USD"),
                amount_minor,
                Decimal(str(credits)),
                idempotency_key,
                json.dumps(
                    {
                        "provider": "google_play",
                        "google_product_id": google_product_id,
                        "internal_pack_code": internal_pack_code,
                        "purchase_token_hash": purchase_token_hash,
                        "order_id": order_id,
                        "package_name": package_name,
                        "country_code": country_code,
                        "fulfillment_source": "wallet_order",
                        "fulfillment_status": "pending",
                    },
                    default=str,
                ),
            )
            wallet_order_id = str(row["id"]) if row else None

    return {
        "wallet_order_id": wallet_order_id,
        "granted_credits": credits,
        "fulfillment_state": "pending",
    }


async def find_existing_google_wallet_order_id(
    conn,
    *,
    user_id: UUID,
    purchase_token_hash: str,
) -> Optional[str]:
    row = await conn.fetchrow(
        """
        select id
        from public.payment_wallet_orders
        where user_id = $1
          and idempotency_key = $2
        limit 1
        """,
        user_id,
        f"google_play:{purchase_token_hash}",
    )
    return str(row["id"]) if row and row.get("id") else None


async def find_subscription_user_id(conn, *, purchase_token_hash: str):
    row = await conn.fetchrow(
        """
        select user_id
        from public.payment_plan_subscriptions
        where gateway_provider = 'google_play'
          and gateway_subscription_id = $1
        limit 1
        """,
        purchase_token_hash,
    )
    return row.get("user_id") if row else None


async def find_purchase_user_id(conn, *, package_name: str, google_product_id: str, purchase_token_hash: str):
    row = await conn.fetchrow(
        """
        select user_id
        from public.google_play_iap_purchases
        where package_name = $1
          and google_product_id = $2
          and purchase_token_hash = $3
        limit 1
        """,
        package_name,
        google_product_id,
        purchase_token_hash,
    )
    return row.get("user_id") if row else None
