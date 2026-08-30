from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from uuid import UUID

from app.services.payments.wallet_fulfillment_service import sync_credit_account_from_lots
from app.services.reservations.reservation_service import _ledger_event

logger = logging.getLogger(__name__)


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


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _to_int_credits(value: Any, default: int = 0) -> int:
    d = _to_decimal(value, str(default))
    try:
        return max(0, int(d.to_integral_value(rounding="ROUND_HALF_UP")))
    except Exception:
        return default


def _clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_dict(value: Any) -> Dict[str, Any]:
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


def _current_cycle_key() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _metadata_external_key(metadata: Dict[str, Any], *, source: str) -> str:
    """Stable idempotency key for the plan-change event.

    Use provider transaction identifiers when available so repeated delivery of
    the same Google/Apple/Stripe event is idempotent, but a later genuine
    re-upgrade in the same billing cycle can still create a new true-up if
    needed.
    """
    for key in (
        "purchase_token_hash",
        "transaction_id",
        "original_transaction_id",
        "gateway_subscription_id",
        "subscription_id",
        "order_id",
        "notification_uuid",
        "message_id",
        "checkout_session_id",
    ):
        value = _clean_text(metadata.get(key))
        if value:
            return value.replace(":", "_")[:96]
    return _clean_text(source, "plan_credit_reconcile").replace(":", "_")[:96]


async def _fetch_plan_guardrail_cap(conn, *, plan_code: str) -> Optional[Decimal]:
    row = await conn.fetchrow(
        """
        select included_credit_cap
        from public.pricing_plan_credit_guardrails
        where lower(plan_code) = lower($1::text)
          and is_active = true
        limit 1
        """,
        plan_code,
    )
    value = _record_get(row, "included_credit_cap")
    return None if value is None else _to_decimal(value)


async def _fetch_active_included_cycle_totals(conn, *, user_id: UUID, cycle_key: str) -> Dict[str, Decimal]:
    row = await conn.fetchrow(
        """
        select
          coalesce(sum(granted_amount), 0)::numeric as granted,
          coalesce(sum(remaining_amount), 0)::numeric as remaining,
          coalesce(sum(reserved_amount), 0)::numeric as reserved
        from public.pricing_credit_lots
        where user_id = $1
          and bucket_type = 'included'
          and status = 'active'
          and (expires_at is null or expires_at > now())
          and metadata_json->>'cycle_key' = $2::text
        """,
        user_id,
        cycle_key,
    )
    return {
        "granted": _to_decimal(_record_get(row, "granted", 0)),
        "remaining": _to_decimal(_record_get(row, "remaining", 0)),
        "reserved": _to_decimal(_record_get(row, "reserved", 0)),
    }


async def _expire_previous_cycle_included_lots(
    conn,
    *,
    user_id: UUID,
    cycle_key: str,
    current_period_start: Optional[datetime],
) -> int:
    """Expire only unreserved included lots from older cycles.

    Reserved old-cycle lots are intentionally left active so in-flight jobs can
    commit or release normally. They remain visible as reserved account balance
    until the reservation lifecycle settles them.
    """
    result = await conn.execute(
        """
        update public.pricing_credit_lots
        set status = 'expired',
            remaining_amount = 0,
            metadata_json = coalesce(metadata_json, '{}'::jsonb)
              || jsonb_build_object(
                   'expired_reason', 'plan_cycle_rollover',
                   'expired_by_cycle_key', $2::text,
                   'expired_at', now()
                 ),
            updated_at = now()
        where user_id = $1
          and bucket_type = 'included'
          and status = 'active'
          and (
            expires_at <= now()
            or (
              coalesce(metadata_json, '{}'::jsonb) ? 'cycle_key'
              and coalesce(metadata_json->>'cycle_key', '') <> $2::text
            )
            or (
              not (coalesce(metadata_json, '{}'::jsonb) ? 'cycle_key')
              and $3::timestamptz is not null
              and coalesce(granted_at, created_at) < $3::timestamptz
            )
          )
          and coalesce(reserved_amount, 0) = 0
        """,
        user_id,
        cycle_key,
        current_period_start,
    )
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0


async def _adopt_legacy_included_lots_for_cycle(
    conn,
    *,
    user_id: UUID,
    cycle_key: str,
    plan_code: str,
    source: str,
    current_period_start: Optional[datetime],
) -> int:
    result = await conn.execute(
        """
        update public.pricing_credit_lots
        set metadata_json = coalesce(metadata_json, '{}'::jsonb)
              || jsonb_build_object(
                   'cycle_key', $2::text,
                   'adopted_by_reconciler', true,
                   'adopted_plan_code', $3::text,
                   'adopted_source', $4::text,
                   'adopted_at', now()
                 ),
            updated_at = now()
        where user_id = $1
          and bucket_type = 'included'
          and status = 'active'
          and (expires_at is null or expires_at > now())
          and not (coalesce(metadata_json, '{}'::jsonb) ? 'cycle_key')
          and (
            $5::timestamptz is null
            or coalesce(granted_at, created_at) >= $5::timestamptz
          )
        """,
        user_id,
        cycle_key,
        plan_code,
        source,
        current_period_start,
    )
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0


async def _reduce_included_unspent(
    conn,
    *,
    user_id: UUID,
    credits_to_reduce: Decimal,
    cycle_key: str,
    reason: str,
) -> Decimal:
    """Reduce unreserved included remaining amount for immediate downgrades.

    Purchased/top-up lots are intentionally never touched. Reserved included
    credits are not forcibly removed; active reservations complete or release
    normally.
    """
    remaining_to_reduce = max(Decimal("0"), credits_to_reduce)
    if remaining_to_reduce <= 0:
        return Decimal("0")

    lots = await conn.fetch(
        """
        select id, remaining_amount, reserved_amount
        from public.pricing_credit_lots
        where user_id = $1
          and bucket_type = 'included'
          and status = 'active'
          and (expires_at is null or expires_at > now())
          and metadata_json->>'cycle_key' = $2::text
        order by granted_at desc nulls last, created_at desc
        for update
        """,
        user_id,
        cycle_key,
    )

    reduced = Decimal("0")
    for lot in lots:
        if remaining_to_reduce <= 0:
            break
        remaining_amount = _to_decimal(_record_get(lot, "remaining_amount", 0))
        reserved_amount = _to_decimal(_record_get(lot, "reserved_amount", 0))
        reducible = max(Decimal("0"), remaining_amount - reserved_amount)
        if reducible <= 0:
            continue
        take = min(reducible, remaining_to_reduce)
        await conn.execute(
            """
            update public.pricing_credit_lots
            set remaining_amount = greatest(reserved_amount, remaining_amount - $2),
                metadata_json = coalesce(metadata_json, '{}'::jsonb)
                  || jsonb_build_object(
                       'last_reduction_reason', $3::text,
                       'last_reduction_amount', $2::text,
                       'last_reduction_at', now()
                     ),
                updated_at = now()
            where id = $1
            """,
            _record_get(lot, "id"),
            take,
            reason,
        )
        reduced += take
        remaining_to_reduce -= take

    return reduced


async def reconcile_included_plan_credits(
    conn,
    *,
    user_id: UUID,
    plan_code: str,
    tier_code: Optional[str] = None,
    included_credit_cap: Optional[Any] = None,
    cycle_key: Optional[str] = None,
    current_period_start: Optional[datetime] = None,
    current_period_end: Optional[datetime] = None,
    source: str = "plan_credit_reconcile",
    metadata_json: Optional[Dict[str, Any]] = None,
    downgrade_policy: str = "preserve_used_reduce_unreserved",
) -> Dict[str, Any]:
    """Reconcile included plan-credit lots after a plan entitlement change.

    Production invariants:
    - Included plan credits are represented by pricing_credit_lots.bucket_type='included'.
    - Paid top-ups are represented by bucket_type='purchased' and are never modified here.
    - Existing current-cycle included-credit usage is preserved across upgrades/downgrades.
    - pricing_credit_accounts is rebuilt from active lots after reconciliation.
    """
    normalized_plan_code = _clean_text(plan_code, "free").lower()
    normalized_tier_code = _clean_text(tier_code, "free").lower()
    effective_cycle_key = _clean_text(cycle_key) or _current_cycle_key()
    md = _as_dict(metadata_json)

    # Best-effort user-level serialization when callers are inside a transaction.
    try:
        await conn.execute("select pg_advisory_xact_lock(hashtextextended($1::text, 0))", str(user_id))
    except Exception:
        logger.debug("plan_credit_reconcile_advisory_lock_skipped", exc_info=True)

    cap = _to_decimal(included_credit_cap) if included_credit_cap is not None else None
    if cap is None:
        cap = await _fetch_plan_guardrail_cap(conn, plan_code=normalized_plan_code)
    if cap is None:
        cap = Decimal("100") if normalized_plan_code == "free" else Decimal("0")
    cap = max(Decimal("0"), cap)

    expired_previous_cycle = await _expire_previous_cycle_included_lots(
        conn,
        user_id=user_id,
        cycle_key=effective_cycle_key,
        current_period_start=current_period_start,
    )
    adopted_legacy = await _adopt_legacy_included_lots_for_cycle(
        conn,
        user_id=user_id,
        cycle_key=effective_cycle_key,
        plan_code=normalized_plan_code,
        source=source,
        current_period_start=current_period_start,
    )

    totals = await _fetch_active_included_cycle_totals(conn, user_id=user_id, cycle_key=effective_cycle_key)
    current_granted = totals["granted"]
    current_remaining = totals["remaining"]
    current_reserved = totals["reserved"]

    included_used = max(Decimal("0"), current_granted - current_remaining - current_reserved)
    target_unspent = max(Decimal("0"), cap - included_used)
    current_unspent = max(Decimal("0"), current_remaining + current_reserved)
    delta = target_unspent - current_unspent

    action = "noop"
    lot_id: Optional[str] = None
    reduced_amount = Decimal("0")

    if delta > 0:
        external_key = _metadata_external_key(md, source=source)
        source_ref = (
            f"plan_credit_reconcile:{user_id}:{effective_cycle_key}:"
            f"{normalized_plan_code}:{external_key}:{int(cap)}"
        )
        inserted = await conn.fetchrow(
            """
            insert into public.pricing_credit_lots(
              user_id, bucket_type, source_type, source_ref, plan_code_at_grant,
              granted_amount, remaining_amount, reserved_amount, granted_at, expires_at,
              status, metadata_json, created_at, updated_at
            )
            values(
              $1, 'included', 'plan_grant', $2::text, $3::text,
              $4::numeric, $4::numeric, 0, now(), $5::timestamptz,
              'active', $6::jsonb, now(), now()
            )
            on conflict do nothing
            returning id
            """,
            user_id,
            source_ref,
            normalized_plan_code,
            delta,
            current_period_end,
            json.dumps(
                {
                    **md,
                    "cycle_key": effective_cycle_key,
                    "source": source,
                    "source_ref": source_ref,
                    "reason": "plan_credit_trueup",
                    "target_plan_code": normalized_plan_code,
                    "target_tier_code": normalized_tier_code,
                    "target_included_cap": str(cap),
                    "included_used_preserved": str(included_used),
                    "previous_included_remaining": str(current_remaining),
                    "previous_included_reserved": str(current_reserved),
                    "trueup_amount": str(delta),
                    "current_period_start": current_period_start.isoformat() if current_period_start else None,
                    "current_period_end": current_period_end.isoformat() if current_period_end else None,
                },
                default=str,
            ),
        )
        lot_id = str(_record_get(inserted, "id")) if inserted and _record_get(inserted, "id") else None
        action = "granted_trueup" if lot_id else "trueup_already_recorded"

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="plan_credit_trueup_grant",
            credits_delta=_to_int_credits(delta),
            idempotency_key=source_ref,
            channel="billing",
            metadata={
                "credit_lot_id": lot_id,
                "cycle_key": effective_cycle_key,
                "plan_code": normalized_plan_code,
                "tier_code": normalized_tier_code,
                "source": source,
                "source_ref": source_ref,
                "included_used_preserved": str(included_used),
                "target_included_cap": str(cap),
            },
            settlement_mode="credits",
            service_name="svc-pricing",
            service_action="plan_credit_reconcile",
        )
    elif delta < 0 and downgrade_policy == "preserve_used_reduce_unreserved":
        reduced_amount = await _reduce_included_unspent(
            conn,
            user_id=user_id,
            credits_to_reduce=abs(delta),
            cycle_key=effective_cycle_key,
            reason=f"plan_credit_reconcile:{normalized_plan_code}",
        )
        if reduced_amount > 0:
            action = "reduced_unreserved"
            await _ledger_event(
                conn,
                user_id=user_id,
                event_type="plan_credit_downgrade_adjustment",
                credits_delta=-_to_int_credits(reduced_amount),
                idempotency_key=(
                    f"plan_credit_reduce:{user_id}:{effective_cycle_key}:"
                    f"{normalized_plan_code}:{_metadata_external_key(md, source=source)}:{int(cap)}"
                ),
                channel="billing",
                metadata={
                    "cycle_key": effective_cycle_key,
                    "plan_code": normalized_plan_code,
                    "tier_code": normalized_tier_code,
                    "source": source,
                    "target_included_cap": str(cap),
                    "included_used_preserved": str(included_used),
                    "requested_reduction": str(abs(delta)),
                    "actual_reduction": str(reduced_amount),
                },
                settlement_mode="credits",
                service_name="svc-pricing",
                service_action="plan_credit_reconcile",
            )

    post_totals = await _fetch_active_included_cycle_totals(conn, user_id=user_id, cycle_key=effective_cycle_key)
    post_granted = post_totals["granted"]
    post_remaining = post_totals["remaining"]
    post_reserved = post_totals["reserved"]
    post_used = max(Decimal("0"), post_granted - post_remaining - post_reserved)
    entitlement_remaining = max(Decimal("0"), cap - post_used)

    await conn.execute(
        """
        with target as (
          select id
          from public.billing_entitlements
          where user_id = $1
            and effective_from <= now()
            and (effective_to is null or effective_to > now())
          order by effective_from desc, updated_at desc
          limit 1
        )
        update public.billing_entitlements be
        set plan_code = $2::text,
            included_credits_total = $3::numeric,
            included_credits_remaining = $4::numeric,
            wallet_topup_allowed = true,
            metadata_json = coalesce(be.metadata_json, '{}'::jsonb)
              || jsonb_build_object(
                   'cycle_key', $5::text,
                   'last_plan_credit_reconcile_at', now(),
                   'last_plan_credit_reconcile_source', $6::text,
                   'last_plan_credit_reconcile_action', $7::text
                 ),
            updated_at = now()
        from target
        where be.id = target.id
        """,
        user_id,
        normalized_plan_code,
        cap,
        entitlement_remaining,
        effective_cycle_key,
        source,
        action,
    )

    account = await sync_credit_account_from_lots(conn, user_id=user_id)

    return {
        "user_id": str(user_id),
        "plan_code": normalized_plan_code,
        "tier_code": normalized_tier_code,
        "cycle_key": effective_cycle_key,
        "included_credit_cap": str(cap),
        "included_used": str(post_used),
        "included_remaining": str(post_remaining),
        "included_reserved": str(post_reserved),
        "target_unspent": str(target_unspent),
        "delta": str(delta),
        "action": action,
        "credit_lot_id": lot_id,
        "reduced_amount": str(reduced_amount),
        "expired_previous_cycle_lots": expired_previous_cycle,
        "adopted_legacy_lots": adopted_legacy,
        "account": account,
    }
