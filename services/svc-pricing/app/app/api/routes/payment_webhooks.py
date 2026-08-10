from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from app.db import ensure_db_pool
from app.services.gateways.stripe_gateway import StripeGateway, StripeSignatureError
from app.services.payments.wallet_fulfillment_service import fulfill_wallet_topup_order
from app.services.entitlement_sync_service import sync_subscription_and_entitlement
from app.services.entitlements.plan_credit_reconciliation_service import (
    reconcile_included_plan_credits,
)

router = APIRouter(tags=["payment-webhooks"])


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


def _to_int_credits(x: Any) -> int:
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return 0
    if d <= 0:
        return 0
    return int(d)


def _to_decimal_or_none(x: Any) -> Optional[Decimal]:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _stripe_cycle_key(*, plan_code: Optional[str], period_start: Any, period_end: Any) -> str:
    anchor = period_start or period_end
    if not anchor:
        from datetime import datetime, timezone

        anchor = datetime.now(timezone.utc)
    interval = "yearly" if "year" in str(plan_code or "").strip().lower() else "monthly"
    if interval == "yearly":
        return f"{anchor.year:04d}"
    return f"{anchor.year:04d}-{anchor.month:02d}"


def _metadata_cycle_key(value: Any) -> Optional[str]:
    md = _as_dict_loose(value)
    cycle_key = str(md.get("cycle_key") or "").strip()
    return cycle_key or None


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
        return


def _recipient_entries(user_id: Optional[str], *, push: bool = True, email: bool = True, in_app: bool = True) -> List[Dict[str, Any]]:
    uid = str(user_id or "").strip()
    if not uid:
        return []
    return [
        {
            "user_id": uid,
            "channels": {
                "in_app": bool(in_app),
                "push": bool(push),
                "email": bool(email),
            },
        }
    ]


def _money_major_from_minor(amount_minor: Any) -> str:
    try:
        minor = int(amount_minor or 0)
    except Exception:
        minor = 0
    major = (Decimal(minor) / Decimal("100")).quantize(Decimal("0.01"))
    return format(major, "f")


def _money_label_from_minor(amount_minor: Any, currency: Optional[str]) -> str:
    ccy = str(currency or "USD").strip().upper() or "USD"
    symbol = "$" if ccy == "USD" else "₹" if ccy == "INR" else f"{ccy} "
    return f"{symbol}{_money_major_from_minor(amount_minor)}"


def _plan_display_name(plan_code: Optional[str]) -> str:
    code = str(plan_code or "").strip().lower()
    if not code:
        return "your plan"
    if code == "free":
        return "Free"
    parts = [part for part in code.replace("-", "_").split("_") if part and not part.endswith("v1")]
    cleaned = [p for p in parts if p not in {"v1", "v2"}]
    return " ".join(cleaned).title() or code


async def _fetch_subscription_notification_context(
    conn,
    *,
    subscription_id: Optional[str] = None,
    fallback_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    row = None
    if subscription_id:
        row = await conn.fetchrow(
            '''
            select
              user_id::text as user_id,
              gateway_subscription_id,
              plan_code,
              subscription_state,
              entitlement_state,
              cancel_at_period_end,
              current_period_end,
              latest_invoice_status
            from payment_plan_subscriptions
            where gateway_subscription_id = $1
            order by updated_at desc, created_at desc
            limit 1
            ''',
            subscription_id,
        )
    if not row and fallback_user_id:
        row = await conn.fetchrow(
            '''
            select
              user_id::text as user_id,
              gateway_subscription_id,
              plan_code,
              subscription_state,
              entitlement_state,
              cancel_at_period_end,
              current_period_end,
              latest_invoice_status
            from payment_plan_subscriptions
            where user_id = $1::uuid
            order by updated_at desc, created_at desc
            limit 1
            ''',
            fallback_user_id,
        )

    out = _as_dict_loose(row)
    user_id = str(out.get("user_id") or fallback_user_id or "").strip() or None
    if user_id:
        ent = await conn.fetchrow(
            '''
            select tier_code, plan_code
            from billing_entitlements
            where user_id = $1::uuid
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc, updated_at desc
            limit 1
            ''',
            user_id,
        )
        ent_dict = _as_dict_loose(ent)
        if ent_dict:
            out["tier_code"] = ent_dict.get("tier_code")
            if not out.get("plan_code"):
                out["plan_code"] = ent_dict.get("plan_code")
    if user_id:
        out["user_id"] = user_id
    return out


async def _record_webhook_event(conn, *, event: Dict[str, Any], headers: Dict[str, Any]) -> str:
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    api_version = str(event.get("api_version") or "") or None
    livemode = bool(event.get("livemode") or False)
    obj = _as_dict_loose(_as_dict_loose(event.get("data")).get("object"))
    object_id = str(obj.get("id") or "") or None
    object_type = str(obj.get("object") or "") or None

    row = await conn.fetchrow(
        '''
        insert into payment_gateway_webhook_events(
          gateway_provider, gateway_event_id, event_type, api_version,
          livemode, object_id, object_type, payload_json, headers_json,
          process_status, received_at
        )
        values('stripe', $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, 'received', now())
        on conflict (gateway_event_id)
        do update set gateway_event_id = payment_gateway_webhook_events.gateway_event_id
        returning process_status
        ''',
        event_id,
        event_type,
        api_version,
        livemode,
        object_id,
        object_type,
        json.dumps(event, default=str),
        json.dumps(headers, default=str),
    )
    return str(row["process_status"] or "received")


async def _mark_webhook_status(conn, *, event_id: str, status: str, failure_reason: Optional[str] = None) -> None:
    await conn.execute(
        '''
        update payment_gateway_webhook_events
        set process_status = $2,
            processed_at = case when $2 in ('processed', 'ignored') then now() else processed_at end,
            failure_reason = $3
        where gateway_event_id = $1
        ''',
        event_id,
        status,
        failure_reason,
    )


async def _get_existing_event_status(conn, *, event_id: str) -> Optional[str]:
    row = await conn.fetchrow(
        "select process_status from payment_gateway_webhook_events where gateway_event_id = $1 limit 1",
        event_id,
    )
    return str(row["process_status"] or "") if row else None


async def _attach_local_subscription_id(conn, *, session_id: Optional[str], subscription_id: Optional[str]) -> None:
    if not session_id or not subscription_id:
        return
    sub_row = await conn.fetchrow(
        "select id from payment_plan_subscriptions where gateway_subscription_id = $1 limit 1",
        subscription_id,
    )
    if not sub_row:
        return
    await conn.execute(
        "update payment_gateway_checkout_sessions set local_subscription_id = $2, updated_at = now() where gateway_checkout_session_id = $1",
        session_id,
        sub_row["id"],
    )


async def _hydrate_subscription(
    gw: StripeGateway,
    *,
    subscription_id: Optional[str],
    fallback_subscription: Optional[Dict[str, Any]] = None,
    require_full: bool = False,
) -> Optional[Dict[str, Any]]:
    if not subscription_id:
        if require_full:
            raise RuntimeError("stripe_subscription_id_missing")
        return fallback_subscription

    try:
        full_subscription = await gw.retrieve_subscription(subscription_id)
    except Exception as exc:
        if require_full:
            raise RuntimeError(f"stripe_subscription_hydration_failed:{subscription_id}:{exc}") from exc
        return fallback_subscription

    if isinstance(full_subscription, dict) and full_subscription.get("id"):
        return full_subscription

    if require_full:
        raise RuntimeError(f"stripe_subscription_hydration_invalid:{subscription_id}")
    return fallback_subscription


def _extract_invoice_subscription_context(invoice_obj: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    parent = _as_dict_loose(invoice_obj.get("parent"))
    subscription_details = _as_dict_loose(parent.get("subscription_details"))

    subscription_id = None
    if str(parent.get("type") or "").strip().lower() == "subscription_details":
        subscription_id = str(subscription_details.get("subscription") or "").strip() or None
    if not subscription_id:
        subscription_id = str(invoice_obj.get("subscription") or "").strip() or None

    fallback_user_id = None
    metadata = _as_dict_loose(subscription_details.get("metadata"))
    if metadata:
        fallback_user_id = str(metadata.get("df_user_id") or "").strip() or None
    if not fallback_user_id:
        invoice_metadata = _as_dict_loose(invoice_obj.get("metadata"))
        fallback_user_id = str(invoice_metadata.get("df_user_id") or "").strip() or None

    return subscription_id, fallback_user_id


async def _fetch_plan_credit_reconciliation_context(
    conn,
    *,
    subscription_id: Optional[str],
    fallback_user_id: Optional[str],
) -> Dict[str, Any]:
    """Read the canonical post-sync entitlement state for plan-credit reconciliation.

    Stripe webhooks may arrive as checkout, subscription, or invoice events.
    After sync_subscription_and_entitlement has written the subscription and
    entitlement rows, this function intentionally reads the database state back
    rather than trusting a particular Stripe payload shape. That keeps the
    reconciler aligned with the same plan identity surfaced by
    /api/payments/overview.
    """
    sub = None
    if subscription_id:
        sub = await conn.fetchrow(
            '''
            select
              user_id,
              gateway_subscription_id,
              plan_code,
              subscription_state,
              entitlement_state,
              current_period_start,
              current_period_end,
              metadata_json,
              updated_at
            from public.payment_plan_subscriptions
            where gateway_subscription_id = $1
            order by updated_at desc, created_at desc
            limit 1
            ''',
            subscription_id,
        )
    if not sub and fallback_user_id:
        sub = await conn.fetchrow(
            '''
            select
              user_id,
              gateway_subscription_id,
              plan_code,
              subscription_state,
              entitlement_state,
              current_period_start,
              current_period_end,
              metadata_json,
              updated_at
            from public.payment_plan_subscriptions
            where user_id = $1::uuid
            order by updated_at desc, created_at desc
            limit 1
            ''',
            fallback_user_id,
        )

    sub_dict = _as_dict_loose(sub)
    user_id = _to_uuid_or_none(sub_dict.get("user_id") or fallback_user_id)
    if user_id is None:
        raise RuntimeError("stripe_plan_credit_reconcile_user_missing")

    ent = await conn.fetchrow(
        '''
        select
          user_id,
          tier_code,
          plan_code,
          included_credits_total,
          included_credits_remaining,
          metadata_json,
          effective_from,
          effective_to,
          updated_at
        from public.billing_entitlements
        where user_id = $1
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by effective_from desc, updated_at desc
        limit 1
        ''',
        user_id,
    )
    ent_dict = _as_dict_loose(ent)

    plan_code = str(ent_dict.get("plan_code") or sub_dict.get("plan_code") or "free").strip().lower() or "free"
    tier_code = str(ent_dict.get("tier_code") or "").strip().lower()
    if not tier_code:
        if plan_code.startswith("enterprise"):
            tier_code = "enterprise"
        elif plan_code.startswith("business") or plan_code.startswith("team"):
            tier_code = "business"
        elif plan_code.startswith("pro"):
            tier_code = "pro"
        else:
            tier_code = "free"

    included_cap = _to_decimal_or_none(ent_dict.get("included_credits_total"))
    if included_cap is not None and included_cap <= 0 and plan_code != "free":
        included_cap = None

    period_start = sub_dict.get("current_period_start")
    period_end = sub_dict.get("current_period_end")
    cycle_key = (
        _metadata_cycle_key(ent_dict.get("metadata_json"))
        or _metadata_cycle_key(sub_dict.get("metadata_json"))
        or _stripe_cycle_key(plan_code=plan_code, period_start=period_start, period_end=period_end)
    )

    return {
        "user_id": user_id,
        "plan_code": plan_code,
        "tier_code": tier_code,
        "included_credit_cap": included_cap,
        "cycle_key": cycle_key,
        "current_period_start": period_start,
        "current_period_end": period_end,
        "subscription": sub_dict,
        "entitlement": ent_dict,
    }


async def _reconcile_stripe_plan_credits_after_sync(
    conn,
    *,
    subscription_id: Optional[str],
    fallback_user_id: Optional[str],
    source: str,
    latest_invoice_status: Optional[str],
) -> Dict[str, Any]:
    ctx = await _fetch_plan_credit_reconciliation_context(
        conn,
        subscription_id=subscription_id,
        fallback_user_id=fallback_user_id,
    )
    sub = ctx.get("subscription") or {}
    ent = ctx.get("entitlement") or {}
    metadata_json = {
        "provider": "stripe",
        "source": source,
        "latest_invoice_status": latest_invoice_status,
        "gateway_subscription_id": str(sub.get("gateway_subscription_id") or subscription_id or ""),
        "subscription_state": str(sub.get("subscription_state") or ""),
        "entitlement_state": str(sub.get("entitlement_state") or ""),
        "entitlement_plan_code": str(ent.get("plan_code") or ""),
        "entitlement_tier_code": str(ent.get("tier_code") or ""),
    }
    return await reconcile_included_plan_credits(
        conn,
        user_id=ctx["user_id"],
        plan_code=ctx["plan_code"],
        tier_code=ctx["tier_code"],
        included_credit_cap=ctx["included_credit_cap"],
        cycle_key=ctx["cycle_key"],
        current_period_start=ctx["current_period_start"],
        current_period_end=ctx["current_period_end"],
        source=source,
        metadata_json=metadata_json,
    )


async def _sync_hydrated_subscription_or_raise(
    conn,
    *,
    gw: StripeGateway,
    subscription_id: Optional[str],
    fallback_subscription: Optional[Dict[str, Any]],
    fallback_user_id: Optional[str] = None,
    latest_invoice_status: Optional[str] = None,
    require_full: bool = True,
    source: str = "stripe_webhook",
    reconcile_plan_credits: bool = True,
) -> Dict[str, Any]:
    hydrated = await _hydrate_subscription(
        gw,
        subscription_id=subscription_id,
        fallback_subscription=fallback_subscription,
        require_full=require_full,
    )
    if not hydrated:
        raise RuntimeError("stripe_subscription_missing_after_hydration")

    await sync_subscription_and_entitlement(
        conn,
        subscription=hydrated,
        fallback_user_id=fallback_user_id,
        latest_invoice_status=latest_invoice_status,
    )

    effective_subscription_id = str(hydrated.get("id") or subscription_id or "").strip() or subscription_id
    if not reconcile_plan_credits:
        return {
            "action": "plan_credit_reconcile_skipped",
            "reason": "non_granting_subscription_event",
            "subscription_id": effective_subscription_id,
            "latest_invoice_status": latest_invoice_status,
        }

    return await _reconcile_stripe_plan_credits_after_sync(
        conn,
        subscription_id=effective_subscription_id,
        fallback_user_id=fallback_user_id,
        source=source,
        latest_invoice_status=latest_invoice_status,
    )


async def _fulfill_wallet_topup(conn, *, session: Dict[str, Any], gateway_event_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    metadata = _as_dict_loose(session.get("metadata"))
    wallet_order_id = _to_uuid_or_none(metadata.get("df_wallet_order_id") or session.get("client_reference_id"))
    if not wallet_order_id:
        return None

    session_id = str(session.get("id") or "")
    amount_minor = int(session.get("amount_total") or 0) if session.get("amount_total") is not None else 0
    currency = str(session.get("currency") or "USD").upper() or "USD"

    await conn.execute(
        '''
        update payment_gateway_checkout_sessions
        set status = 'complete',
            completed_at = now(),
            currency = coalesce($3, currency),
            amount_minor = coalesce($4, amount_minor),
            updated_at = now(),
            metadata_json = $2::jsonb
        where gateway_checkout_session_id = $1
        ''',
        session_id,
        json.dumps({"stripe": session}, default=str),
        currency,
        amount_minor if amount_minor > 0 else None,
    )

    return await fulfill_wallet_topup_order(
        conn,
        wallet_order_id=wallet_order_id,
        gateway_provider="stripe",
        gateway_checkout_session_id=session_id or None,
        gateway_event_id=gateway_event_id,
        source_metadata={"stripe_session_id": session_id, "stripe": session},
    )


@router.post("/api/payments/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(default=None, alias="Stripe-Signature"),
):
    gw = StripeGateway()
    raw_body = await request.body()
    try:
        event = gw.verify_webhook_signature(raw_body=raw_body, stripe_signature=stripe_signature or "")
    except StripeSignatureError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    headers = {k: v for k, v in request.headers.items()}
    pool = await ensure_db_pool()
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    pending_notifications: List[Dict[str, Any]] = []

    async with pool.acquire() as conn:
        existing_status = await _get_existing_event_status(conn, event_id=event_id)
        if existing_status in {"processed", "ignored"}:
            return {"ok": True, "event_id": event_id, "event_type": event_type, "duplicate": True}

        try:
            await _record_webhook_event(conn, event=event, headers=headers)
        except Exception:
            pass

        obj = _as_dict_loose(_as_dict_loose(event.get("data")).get("object"))
        try:
            if event_type == "checkout.session.completed":
                metadata = _as_dict_loose(obj.get("metadata"))
                purpose = str(metadata.get("df_order_type") or "")
                session_id = str(obj.get("id") or "")
                currency = str(obj.get("currency") or "").upper() or None
                amount_minor = int(obj.get("amount_total") or 0) if obj.get("amount_total") is not None else None

                await conn.execute(
                    '''
                    update payment_gateway_checkout_sessions
                    set status = 'complete',
                        completed_at = now(),
                        currency = coalesce($3, currency),
                        amount_minor = coalesce($4, amount_minor),
                        updated_at = now(),
                        metadata_json = $2::jsonb
                    where gateway_checkout_session_id = $1
                    ''',
                    session_id,
                    json.dumps({"stripe": obj}, default=str),
                    currency,
                    amount_minor,
                )

                if purpose == "wallet_topup":
                    topup_ctx = await _fulfill_wallet_topup(conn, session=obj, gateway_event_id=event_id)
                    if topup_ctx and _recipient_entries(topup_ctx.get("user_id")):
                        credits = int(topup_ctx.get("credits_to_grant") or 0)
                        pending_notifications.append(
                            {
                                "payload": {
                                    "event_type": "PAYMENT_SUCCESS",
                                    "category": "billing",
                                    "priority": "important",
                                    "source_service": "svc-pricing",
                                    "source_ref_type": "wallet_order",
                                    "source_ref_id": str(topup_ctx.get("wallet_order_id") or ""),
                                    "actor_user_id": None,
                                    "title": "Top-up completed",
                                    "body": f"Your payment was received and {credits} credits were added to your account.",
                                    "action_route": "/pricing/plan-billing",
                                    "action_label": "View billing",
                                    "image_url": None,
                                    "payload_json": {
                                        "wallet_order_id": str(topup_ctx.get("wallet_order_id") or ""),
                                        "checkout_session_id": str(topup_ctx.get("gateway_checkout_session_id") or ""),
                                        "credits_to_grant": credits,
                                        "amount_minor": int(topup_ctx.get("amount_minor") or 0),
                                        "currency": str(topup_ctx.get("currency") or "USD"),
                                    },
                                    "metadata_json": {
                                        "wallet_order_id": str(topup_ctx.get("wallet_order_id") or ""),
                                        "credits_to_grant": credits,
                                        "amount_minor": int(topup_ctx.get("amount_minor") or 0),
                                        "currency": str(topup_ctx.get("currency") or "USD"),
                                    },
                                    "dedupe_key": f"payment-success:wallet-topup:{event_id}",
                                    "recipients": _recipient_entries(str(topup_ctx.get("user_id") or "")),
                                },
                                "context": {
                                    "event_id": event_id,
                                    "event_type": event_type,
                                    "user_id": str(topup_ctx.get("user_id") or ""),
                                    "wallet_order_id": str(topup_ctx.get("wallet_order_id") or ""),
                                },
                            }
                        )

                elif purpose in {"plan_subscription", "plan_upgrade", "plan_downgrade"}:
                    sub_id = str(obj.get("subscription") or "").strip() or None
                    fallback_user_id = str(metadata.get("df_user_id") or "").strip() or None
                    await _sync_hydrated_subscription_or_raise(
                        conn,
                        gw=gw,
                        subscription_id=sub_id,
                        fallback_subscription=None,
                        fallback_user_id=fallback_user_id,
                        latest_invoice_status="checkout_completed",
                        require_full=True,
                        source="stripe_checkout_subscription",
                    )
                    await _attach_local_subscription_id(
                        conn,
                        session_id=session_id,
                        subscription_id=sub_id,
                    )
                    sub_ctx = await _fetch_subscription_notification_context(
                        conn,
                        subscription_id=sub_id,
                        fallback_user_id=fallback_user_id,
                    )
                    if sub_ctx and _recipient_entries(sub_ctx.get("user_id")):
                        plan_name = _plan_display_name(sub_ctx.get("plan_code"))
                        pending_notifications.append(
                            {
                                "payload": {
                                    "event_type": "SUBSCRIPTION_UPDATED",
                                    "category": "billing",
                                    "priority": "important",
                                    "source_service": "svc-pricing",
                                    "source_ref_type": "subscription",
                                    "source_ref_id": str(sub_ctx.get("gateway_subscription_id") or sub_id or ""),
                                    "actor_user_id": None,
                                    "title": "Plan change confirmed",
                                    "body": f"Your checkout completed and {plan_name} is now active on your account.",
                                    "action_route": "/pricing/plan-billing",
                                    "action_label": "View billing",
                                    "image_url": None,
                                    "payload_json": {
                                        "gateway_subscription_id": str(sub_ctx.get("gateway_subscription_id") or sub_id or ""),
                                        "plan_code": str(sub_ctx.get("plan_code") or ""),
                                        "tier_code": str(sub_ctx.get("tier_code") or ""),
                                        "subscription_state": str(sub_ctx.get("subscription_state") or ""),
                                    },
                                    "metadata_json": {
                                        "gateway_subscription_id": str(sub_ctx.get("gateway_subscription_id") or sub_id or ""),
                                        "plan_code": str(sub_ctx.get("plan_code") or ""),
                                        "tier_code": str(sub_ctx.get("tier_code") or ""),
                                        "subscription_state": str(sub_ctx.get("subscription_state") or ""),
                                    },
                                    "dedupe_key": f"subscription-updated:checkout:{event_id}",
                                    "recipients": _recipient_entries(str(sub_ctx.get("user_id") or "")),
                                },
                                "context": {
                                    "event_id": event_id,
                                    "event_type": event_type,
                                    "user_id": str(sub_ctx.get("user_id") or ""),
                                    "subscription_id": str(sub_ctx.get("gateway_subscription_id") or sub_id or ""),
                                },
                            }
                        )

                await _mark_webhook_status(conn, event_id=event_id, status="processed")

            elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
                sub_id = str(obj.get("id") or "").strip() or None
                metadata = _as_dict_loose(obj.get("metadata"))
                fallback_user_id = str(metadata.get("df_user_id") or "").strip() or None
                await _sync_hydrated_subscription_or_raise(
                    conn,
                    gw=gw,
                    subscription_id=sub_id,
                    fallback_subscription=None,
                    fallback_user_id=fallback_user_id,
                    latest_invoice_status=None,
                    require_full=True,
                    source=f"stripe_{event_type}",
                )
                sub_ctx = await _fetch_subscription_notification_context(
                    conn,
                    subscription_id=sub_id,
                    fallback_user_id=fallback_user_id,
                )
                if sub_ctx and _recipient_entries(sub_ctx.get("user_id")):
                    plan_name = _plan_display_name(sub_ctx.get("plan_code"))
                    if event_type == "customer.subscription.deleted":
                        title = "Subscription canceled"
                        body = f"Your {plan_name} subscription has been canceled."
                        emitted_event_type = "SUBSCRIPTION_CANCELED"
                    elif bool(sub_ctx.get("cancel_at_period_end") or False):
                        title = "Cancellation scheduled"
                        period_end = str(sub_ctx.get("current_period_end") or "").strip()
                        body = f"Your {plan_name} subscription will end at the close of the current period." + (f" Effective at {period_end}." if period_end else "")
                        emitted_event_type = "SUBSCRIPTION_CANCELLATION_SCHEDULED"
                    elif event_type == "customer.subscription.created":
                        title = "Subscription active"
                        body = f"Your {plan_name} subscription is now active."
                        emitted_event_type = "SUBSCRIPTION_ACTIVATED"
                    else:
                        title = "Subscription updated"
                        body = f"Your billing subscription was updated. Current plan: {plan_name}."
                        emitted_event_type = "SUBSCRIPTION_UPDATED"

                    pending_notifications.append(
                        {
                            "payload": {
                                "event_type": emitted_event_type,
                                "category": "billing",
                                "priority": "important",
                                "source_service": "svc-pricing",
                                "source_ref_type": "subscription",
                                "source_ref_id": str(sub_ctx.get("gateway_subscription_id") or sub_id or ""),
                                "actor_user_id": None,
                                "title": title,
                                "body": body,
                                "action_route": "/pricing/plan-billing",
                                "action_label": "View billing",
                                "image_url": None,
                                "payload_json": {
                                    "gateway_subscription_id": str(sub_ctx.get("gateway_subscription_id") or sub_id or ""),
                                    "plan_code": str(sub_ctx.get("plan_code") or ""),
                                    "tier_code": str(sub_ctx.get("tier_code") or ""),
                                    "subscription_state": str(sub_ctx.get("subscription_state") or ""),
                                    "entitlement_state": str(sub_ctx.get("entitlement_state") or ""),
                                    "cancel_at_period_end": bool(sub_ctx.get("cancel_at_period_end") or False),
                                },
                                "metadata_json": {
                                    "gateway_subscription_id": str(sub_ctx.get("gateway_subscription_id") or sub_id or ""),
                                    "plan_code": str(sub_ctx.get("plan_code") or ""),
                                    "tier_code": str(sub_ctx.get("tier_code") or ""),
                                    "subscription_state": str(sub_ctx.get("subscription_state") or ""),
                                    "entitlement_state": str(sub_ctx.get("entitlement_state") or ""),
                                },
                                "dedupe_key": f"subscription:{event_type}:{event_id}",
                                "recipients": _recipient_entries(str(sub_ctx.get("user_id") or "")),
                            },
                            "context": {
                                "event_id": event_id,
                                "event_type": event_type,
                                "user_id": str(sub_ctx.get("user_id") or ""),
                                "subscription_id": str(sub_ctx.get("gateway_subscription_id") or sub_id or ""),
                            },
                        }
                    )
                await _mark_webhook_status(conn, event_id=event_id, status="processed")

            elif event_type == "invoice.paid":
                subscription_id, fallback_user_id = _extract_invoice_subscription_context(obj)
                await _sync_hydrated_subscription_or_raise(
                    conn,
                    gw=gw,
                    subscription_id=subscription_id,
                    fallback_subscription=None,
                    fallback_user_id=fallback_user_id,
                    latest_invoice_status="paid",
                    require_full=True,
                    source="stripe_invoice_paid",
                )
                sub_ctx = await _fetch_subscription_notification_context(
                    conn,
                    subscription_id=subscription_id,
                    fallback_user_id=fallback_user_id,
                )
                if sub_ctx and _recipient_entries(sub_ctx.get("user_id")):
                    amount_paid = int(obj.get("amount_paid") or obj.get("amount_due") or obj.get("total") or 0)
                    currency = str(obj.get("currency") or "USD").upper()
                    plan_name = _plan_display_name(sub_ctx.get("plan_code"))
                    pending_notifications.append(
                        {
                            "payload": {
                                "event_type": "PAYMENT_SUCCESS",
                                "category": "billing",
                                "priority": "important",
                                "source_service": "svc-pricing",
                                "source_ref_type": "invoice",
                                "source_ref_id": str(obj.get("id") or ""),
                                "actor_user_id": None,
                                "title": "Payment received",
                                "body": f"Your payment of {_money_label_from_minor(amount_paid, currency)} for {plan_name} was received successfully.",
                                "action_route": "/pricing/plan-billing",
                                "action_label": "View billing",
                                "image_url": None,
                                "payload_json": {
                                    "invoice_id": str(obj.get("id") or ""),
                                    "gateway_subscription_id": str(sub_ctx.get("gateway_subscription_id") or subscription_id or ""),
                                    "plan_code": str(sub_ctx.get("plan_code") or ""),
                                    "tier_code": str(sub_ctx.get("tier_code") or ""),
                                    "amount_minor": amount_paid,
                                    "currency": currency,
                                },
                                "metadata_json": {
                                    "invoice_id": str(obj.get("id") or ""),
                                    "gateway_subscription_id": str(sub_ctx.get("gateway_subscription_id") or subscription_id or ""),
                                    "plan_code": str(sub_ctx.get("plan_code") or ""),
                                    "tier_code": str(sub_ctx.get("tier_code") or ""),
                                    "amount_minor": amount_paid,
                                    "currency": currency,
                                },
                                "dedupe_key": f"payment-success:invoice:{event_id}",
                                "recipients": _recipient_entries(str(sub_ctx.get("user_id") or "")),
                            },
                            "context": {
                                "event_id": event_id,
                                "event_type": event_type,
                                "user_id": str(sub_ctx.get("user_id") or ""),
                                "invoice_id": str(obj.get("id") or ""),
                            },
                        }
                    )
                await _mark_webhook_status(conn, event_id=event_id, status="processed")

            elif event_type == "invoice.payment_failed":
                subscription_id, fallback_user_id = _extract_invoice_subscription_context(obj)
                await _sync_hydrated_subscription_or_raise(
                    conn,
                    gw=gw,
                    subscription_id=subscription_id,
                    fallback_subscription=None,
                    fallback_user_id=fallback_user_id,
                    latest_invoice_status="payment_failed",
                    require_full=True,
                    source="stripe_invoice_payment_failed",
                    reconcile_plan_credits=False,
                )
                sub_ctx = await _fetch_subscription_notification_context(
                    conn,
                    subscription_id=subscription_id,
                    fallback_user_id=fallback_user_id,
                )
                if sub_ctx and _recipient_entries(sub_ctx.get("user_id")):
                    amount_due = int(obj.get("amount_due") or obj.get("amount_remaining") or obj.get("total") or 0)
                    currency = str(obj.get("currency") or "USD").upper()
                    plan_name = _plan_display_name(sub_ctx.get("plan_code"))
                    pending_notifications.append(
                        {
                            "payload": {
                                "event_type": "PAYMENT_FAILED",
                                "category": "billing",
                                "priority": "important",
                                "source_service": "svc-pricing",
                                "source_ref_type": "invoice",
                                "source_ref_id": str(obj.get("id") or ""),
                                "actor_user_id": None,
                                "title": "Payment failed",
                                "body": f"We could not process your payment of {_money_label_from_minor(amount_due, currency)} for {plan_name}. Please update your billing method.",
                                "action_route": "/pricing/plan-billing",
                                "action_label": "Update billing",
                                "image_url": None,
                                "payload_json": {
                                    "invoice_id": str(obj.get("id") or ""),
                                    "gateway_subscription_id": str(sub_ctx.get("gateway_subscription_id") or subscription_id or ""),
                                    "plan_code": str(sub_ctx.get("plan_code") or ""),
                                    "tier_code": str(sub_ctx.get("tier_code") or ""),
                                    "amount_minor": amount_due,
                                    "currency": currency,
                                },
                                "metadata_json": {
                                    "invoice_id": str(obj.get("id") or ""),
                                    "gateway_subscription_id": str(sub_ctx.get("gateway_subscription_id") or subscription_id or ""),
                                    "plan_code": str(sub_ctx.get("plan_code") or ""),
                                    "tier_code": str(sub_ctx.get("tier_code") or ""),
                                    "amount_minor": amount_due,
                                    "currency": currency,
                                },
                                "dedupe_key": f"payment-failed:invoice:{event_id}",
                                "recipients": _recipient_entries(str(sub_ctx.get("user_id") or "")),
                            },
                            "context": {
                                "event_id": event_id,
                                "event_type": event_type,
                                "user_id": str(sub_ctx.get("user_id") or ""),
                                "invoice_id": str(obj.get("id") or ""),
                            },
                        }
                    )
                await _mark_webhook_status(conn, event_id=event_id, status="processed")

            elif event_type == "checkout.session.expired":
                await conn.execute(
                    "update payment_gateway_checkout_sessions set status = 'expired', updated_at = now(), metadata_json = $2::jsonb where gateway_checkout_session_id = $1",
                    str(obj.get("id") or ""),
                    json.dumps({"stripe": obj}, default=str),
                )
                md = _as_dict_loose(obj.get("metadata"))
                wallet_order_id = _to_uuid_or_none(md.get("df_wallet_order_id") or obj.get("client_reference_id"))
                if wallet_order_id:
                    await conn.execute(
                        "update payment_wallet_orders set payment_state = 'canceled', updated_at = now() where id = $1 and fulfillment_state = 'pending'",
                        wallet_order_id,
                    )
                await _mark_webhook_status(conn, event_id=event_id, status="processed")

            elif event_type == "payment_intent.payment_failed":
                pi_id = str(obj.get("id") or "")
                await conn.execute(
                    "update payment_gateway_payment_intents set status = 'failed', updated_at = now(), metadata_json = $2::jsonb where gateway_payment_intent_id = $1",
                    pi_id,
                    json.dumps({"stripe": obj}, default=str),
                )
                wallet_row = await conn.fetchrow(
                    "select id, user_id, currency, amount_minor from payment_wallet_orders where gateway_payment_intent_id = $1 and fulfillment_state = 'pending' limit 1",
                    pi_id,
                )
                await conn.execute(
                    "update payment_wallet_orders set payment_state = 'failed', updated_at = now() where gateway_payment_intent_id = $1 and fulfillment_state = 'pending'",
                    pi_id,
                )
                if wallet_row and _recipient_entries(str(wallet_row["user_id"])):
                    amount_minor = int(wallet_row["amount_minor"] or 0)
                    currency = str(wallet_row["currency"] or "USD").upper()
                    pending_notifications.append(
                        {
                            "payload": {
                                "event_type": "PAYMENT_FAILED",
                                "category": "billing",
                                "priority": "important",
                                "source_service": "svc-pricing",
                                "source_ref_type": "payment_intent",
                                "source_ref_id": pi_id,
                                "actor_user_id": None,
                                "title": "Payment failed",
                                "body": f"We could not process your top-up payment of {_money_label_from_minor(amount_minor, currency)}.",
                                "action_route": "/pricing/plan-billing",
                                "action_label": "Retry payment",
                                "image_url": None,
                                "payload_json": {
                                    "wallet_order_id": str(wallet_row["id"] or ""),
                                    "payment_intent_id": pi_id,
                                    "amount_minor": amount_minor,
                                    "currency": currency,
                                },
                                "metadata_json": {
                                    "wallet_order_id": str(wallet_row["id"] or ""),
                                    "payment_intent_id": pi_id,
                                    "amount_minor": amount_minor,
                                    "currency": currency,
                                },
                                "dedupe_key": f"payment-failed:intent:{event_id}",
                                "recipients": _recipient_entries(str(wallet_row["user_id"])),
                            },
                            "context": {
                                "event_id": event_id,
                                "event_type": event_type,
                                "user_id": str(wallet_row["user_id"]),
                                "payment_intent_id": pi_id,
                            },
                        }
                    )
                await _mark_webhook_status(conn, event_id=event_id, status="processed")

            else:
                await _mark_webhook_status(conn, event_id=event_id, status="ignored")

        except Exception as exc:
            await _mark_webhook_status(conn, event_id=event_id, status="failed", failure_reason=str(exc))
            raise HTTPException(status_code=500, detail=f"webhook_processing_failed:{exc}")

    for item in pending_notifications:
        try:
            await _emit_notification_best_effort(item["payload"], context=item.get("context") or {"event_id": event_id, "event_type": event_type})
        except Exception:
            pass

    return {"ok": True, "event_id": event_id, "event_type": event_type}
