from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from uuid import UUID

from app.services.reservations.reservation_service import _ensure_account_row, _ledger_event


PAID_PAYMENT_STATES = {"paid", "succeeded", "completed"}
GRANTED_FULFILLMENT_STATES = {"granted", "fulfilled"}


def _record_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        pass
    try:
        value = row.get(key)
        return default if value is None else value
    except Exception:
        return default


def _to_uuid_or_none(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value)) if value else None
    except Exception:
        return None


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _to_int_credits(value: Any) -> int:
    amount = _to_decimal(value)
    if amount <= 0:
        return 0
    return int(amount.to_integral_value(rounding="ROUND_HALF_UP"))


def _as_dict_loose(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


async def sync_credit_account_from_lots(conn, *, user_id: UUID) -> Dict[str, Any]:
    """Rebuild the cached credit account balance from canonical active lots.

    pricing_credit_lots is the source of truth. pricing_credit_accounts is a
    cache used by APIs/guardrails and must be derived from lots so Apple,
    Stripe, and future gateways cannot drift.
    """
    await _ensure_account_row(conn, user_id)
    sums = await conn.fetchrow(
        """
        select
          coalesce(sum(remaining_amount) filter (
            where status = 'active'
              and (expires_at is null or expires_at > now())
          ), 0) as balance_credits,
          coalesce(sum(reserved_amount) filter (
            where status = 'active'
              and (expires_at is null or expires_at > now())
          ), 0) as reserved_credits
        from pricing_credit_lots
        where user_id = $1
        """,
        user_id,
    )
    balance_credits = _to_decimal(_record_get(sums, "balance_credits"))
    reserved_credits = _to_decimal(_record_get(sums, "reserved_credits"))
    row = await conn.fetchrow(
        """
        update pricing_credit_accounts
        set balance_credits = $2,
            reserved_credits = $3,
            updated_at = now()
        where user_id = $1
        returning user_id, balance_credits, reserved_credits, settlement_mode, updated_at
        """,
        user_id,
        balance_credits,
        reserved_credits,
    )
    return {
        "user_id": str(user_id),
        "balance_credits": str(_record_get(row, "balance_credits", balance_credits)),
        "reserved_credits": str(_record_get(row, "reserved_credits", reserved_credits)),
        "settlement_mode": str(_record_get(row, "settlement_mode", "") or ""),
        "updated_at": _record_get(row, "updated_at"),
    }


async def _ensure_wallet_topup_lot(
    conn,
    *,
    wallet_order_id: UUID,
    user_id: UUID,
    credits_to_grant: Decimal,
    plan_code_at_grant: Optional[str],
    metadata: Dict[str, Any],
) -> Optional[str]:
    if credits_to_grant <= 0:
        raise ValueError("wallet_topup_credits_to_grant_must_be_positive")

    inserted = await conn.fetchrow(
        """
        insert into pricing_credit_lots (
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
        ) values (
          $1,
          'purchased',
          'topup',
          $2,
          $3,
          $4,
          $4,
          0,
          now(),
          null,
          'active',
          $5::jsonb,
          now(),
          now()
        )
        on conflict do nothing
        returning id
        """,
        user_id,
        str(wallet_order_id),
        plan_code_at_grant,
        credits_to_grant,
        json.dumps(metadata, default=str),
    )
    if inserted and _record_get(inserted, "id"):
        return str(_record_get(inserted, "id"))

    existing = await conn.fetchrow(
        """
        select id
        from pricing_credit_lots
        where user_id = $1
          and bucket_type = 'purchased'
          and source_type = 'topup'
          and source_ref = $2
        order by created_at desc
        limit 1
        """,
        user_id,
        str(wallet_order_id),
    )
    return str(_record_get(existing, "id")) if existing and _record_get(existing, "id") else None


async def _ensure_wallet_topup_ledger(
    conn,
    *,
    wallet_order_id: UUID,
    user_id: UUID,
    credits_delta: int,
    amount_minor: int,
    currency: str,
    gateway_provider: str,
    gateway_checkout_session_id: Optional[str],
    credit_lot_id: Optional[str],
    source_metadata: Dict[str, Any],
) -> Optional[str]:
    ledger_idempotency_key = f"wallet_topup_grant:{wallet_order_id}"
    existing = await conn.fetchrow(
        "select id from pricing_credit_ledger_events where user_id = $1 and idempotency_key = $2 limit 1",
        user_id,
        ledger_idempotency_key,
    )
    if existing and _record_get(existing, "id"):
        return str(_record_get(existing, "id"))

    account_before = await conn.fetchrow(
        "select balance_credits, reserved_credits from pricing_credit_accounts where user_id = $1 for update",
        user_id,
    )
    balance_before = _to_int_credits(_record_get(account_before, "balance_credits", 0))

    await _ledger_event(
        conn,
        user_id=user_id,
        event_type="wallet_topup_grant",
        credits_delta=credits_delta,
        idempotency_key=ledger_idempotency_key,
        sku_code=None,
        quantity=Decimal("1"),
        country_code=str(source_metadata.get("country_code") or "") or None,
        currency=currency,
        money_amount=None,
        channel="mobile" if str(gateway_provider or "").strip().lower() in {"apple_iap", "google_play"} else "web",
        metadata={
            "wallet_order_id": str(wallet_order_id),
            "credit_lot_id": credit_lot_id,
            "gateway_checkout_session_id": gateway_checkout_session_id,
            "gateway_provider": gateway_provider,
            "amount_minor": amount_minor,
            "currency": currency,
            "balance_before_cache": balance_before,
            **source_metadata,
        },
        settlement_mode="credits",
        service_name="svc-pricing",
        service_action="wallet_topup_grant",
    )
    created = await conn.fetchrow(
        "select id from pricing_credit_ledger_events where user_id = $1 and idempotency_key = $2 limit 1",
        user_id,
        ledger_idempotency_key,
    )
    return str(_record_get(created, "id")) if created and _record_get(created, "id") else None


async def fulfill_wallet_topup_order(
    conn,
    *,
    wallet_order_id: UUID,
    gateway_provider: str,
    gateway_checkout_session_id: Optional[str] = None,
    gateway_event_id: Optional[str] = None,
    gateway_transaction_id: Optional[str] = None,
    source_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Provider-neutral, idempotent wallet top-up fulfillment.

    Apple/Stripe/etc. are responsible only for verifying that a payment is real.
    This function is the only canonical writer for purchased top-up credits:
    pricing_credit_lots, pricing_credit_ledger_events, payment_wallet_orders,
    and the pricing_credit_accounts cache are updated here in one transaction.

    It intentionally repairs partial legacy states: if an order is already marked
    granted but its purchased credit lot is missing, the lot is created and the
    account cache is rebuilt from lots.
    """
    row = await conn.fetchrow(
        """
        select
          id,
          user_id,
          payment_state,
          fulfillment_state,
          credits_to_grant,
          amount_minor,
          ledger_entry_id,
          gateway_checkout_session_id,
          gateway_provider,
          currency,
          metadata_json
        from payment_wallet_orders
        where id = $1
        for update
        """,
        wallet_order_id,
    )
    if not row:
        return None

    user_id = UUID(str(_record_get(row, "user_id")))
    credits_to_grant = _to_decimal(_record_get(row, "credits_to_grant"))
    credits_delta = _to_int_credits(credits_to_grant)
    amount_minor = int(_record_get(row, "amount_minor", 0) or 0)
    currency = str(_record_get(row, "currency", "USD") or "USD").upper()
    provider = str(gateway_provider or _record_get(row, "gateway_provider", "") or "").strip().lower() or "unknown"
    checkout_session_id = str(gateway_checkout_session_id or _record_get(row, "gateway_checkout_session_id", "") or "") or None
    existing_order_md = _as_dict_loose(_record_get(row, "metadata_json"))
    source_md = dict(source_metadata or {})

    merged_meta = {
        **existing_order_md,
        "gateway_provider": provider,
        "gateway_checkout_session_id": checkout_session_id,
        "gateway_event_id": gateway_event_id,
        "gateway_transaction_id": gateway_transaction_id,
        "wallet_order_id": str(wallet_order_id),
        "fulfillment_source": "wallet_fulfillment_service",
        **source_md,
    }

    current_ent = await conn.fetchrow(
        """
        select plan_code
        from billing_entitlements
        where user_id = $1
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by effective_from desc, updated_at desc
        limit 1
        """,
        user_id,
    )
    plan_code_at_grant = str(_record_get(current_ent, "plan_code", "") or "") or None

    await _ensure_account_row(conn, user_id)

    lot_metadata = {
        "provider": provider,
        "wallet_order_id": str(wallet_order_id),
        "gateway_checkout_session_id": checkout_session_id,
        "gateway_event_id": gateway_event_id,
        "gateway_transaction_id": gateway_transaction_id,
        "amount_minor": amount_minor,
        "currency": currency,
        **source_md,
    }
    credit_lot_id = await _ensure_wallet_topup_lot(
        conn,
        wallet_order_id=wallet_order_id,
        user_id=user_id,
        credits_to_grant=credits_to_grant,
        plan_code_at_grant=plan_code_at_grant,
        metadata=lot_metadata,
    )

    existing_ledger_entry_id = _record_get(row, "ledger_entry_id")
    ledger_entry_id = str(existing_ledger_entry_id) if existing_ledger_entry_id else None
    if not ledger_entry_id:
        ledger_entry_id = await _ensure_wallet_topup_ledger(
            conn,
            wallet_order_id=wallet_order_id,
            user_id=user_id,
            credits_delta=credits_delta,
            amount_minor=amount_minor,
            currency=currency,
            gateway_provider=provider,
            gateway_checkout_session_id=checkout_session_id,
            credit_lot_id=credit_lot_id,
            source_metadata=source_md,
        )

    merged_meta.update(
        {
            "credit_lot_id": credit_lot_id,
            "ledger_entry_id": ledger_entry_id,
            "fulfillment_status": "granted",
        }
    )

    await conn.execute(
        """
        update payment_wallet_orders
        set payment_state = 'succeeded',
            fulfillment_state = 'granted',
            ledger_entry_id = coalesce($2, ledger_entry_id),
            gateway_checkout_session_id = coalesce($3, gateway_checkout_session_id),
            metadata_json = $4::jsonb,
            updated_at = now()
        where id = $1
        """,
        wallet_order_id,
        _to_uuid_or_none(ledger_entry_id),
        checkout_session_id,
        json.dumps(merged_meta, default=str),
    )

    account = await sync_credit_account_from_lots(conn, user_id=user_id)

    return {
        "user_id": str(user_id),
        "wallet_order_id": str(wallet_order_id),
        "gateway_provider": provider,
        "gateway_checkout_session_id": checkout_session_id or "",
        "gateway_event_id": gateway_event_id,
        "gateway_transaction_id": gateway_transaction_id,
        "credits_to_grant": credits_delta,
        "amount_minor": amount_minor,
        "currency": currency,
        "payment_state": "succeeded",
        "fulfillment_state": "granted",
        "credit_lot_id": credit_lot_id,
        "ledger_entry_id": ledger_entry_id,
        "account": account,
    }
