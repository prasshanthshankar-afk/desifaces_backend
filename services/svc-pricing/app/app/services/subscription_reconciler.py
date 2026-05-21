from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.services.gateways.stripe_gateway import StripeGateway
from app.services.entitlement_sync_service import sync_subscription_and_entitlement


def _as_dict_loose(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


@dataclass(frozen=True)
class ReconcileCandidate:
    user_id: UUID
    gateway_subscription_id: str
    plan_code: str
    current_period_end: Optional[datetime]
    metadata_json: Dict[str, Any]


async def list_reconcile_candidates(
    conn: asyncpg.Connection,
    *,
    lookahead_minutes: int = 30,
    limit: int = 100,
) -> List[ReconcileCandidate]:
    """
    Candidates:
      - active/grace subscriptions ending soon or already ended
      - rows with pending_change metadata
      - rows whose metadata indicates a scheduled change that should have applied
    """
    rows = await conn.fetch(
        """
        with ranked as (
          select
            p.user_id,
            p.gateway_subscription_id,
            p.plan_code,
            p.current_period_end,
            p.metadata_json,
            row_number() over (
              partition by p.user_id
              order by
                case when p.entitlement_state in ('active', 'grace') then 0 else 1 end,
                p.current_period_end desc nulls last,
                p.updated_at desc
            ) as rn
          from payment_plan_subscriptions p
          where p.gateway_subscription_id is not null
            and (
              p.entitlement_state in ('active', 'grace')
              or p.subscription_state in ('trialing', 'active', 'past_due', 'unpaid', 'paused')
            )
        )
        select
          user_id,
          gateway_subscription_id,
          plan_code,
          current_period_end,
          metadata_json
        from ranked
        where rn = 1
          and (
            current_period_end is null
            or current_period_end <= (now() + ($1::int || ' minutes')::interval)
            or coalesce(metadata_json, '{}'::jsonb) ? 'pending_change'
          )
        order by current_period_end asc nulls first
        limit $2
        """,
        lookahead_minutes,
        limit,
    )
    out: List[ReconcileCandidate] = []
    for row in rows:
        out.append(
            ReconcileCandidate(
                user_id=row["user_id"],
                gateway_subscription_id=str(row["gateway_subscription_id"]),
                plan_code=str(row.get("plan_code") or ""),
                current_period_end=row.get("current_period_end"),
                metadata_json=_as_dict_loose(row.get("metadata_json")),
            )
        )
    return out


async def mark_reconcile_attempt(
    conn: asyncpg.Connection,
    *,
    gateway_subscription_id: str,
    ok: bool,
    message: Optional[str] = None,
) -> None:
    await conn.execute(
        """
        update payment_plan_subscriptions
        set metadata_json = coalesce(metadata_json, '{}'::jsonb)
          || jsonb_build_object(
               'reconciler_last_run_at', now(),
               'reconciler_last_status', case when $2 then 'ok' else 'failed' end,
               'reconciler_last_message', coalesce($3, '')
             ),
            updated_at = now()
        where gateway_subscription_id = $1
        """,
        gateway_subscription_id,
        ok,
        message or "",
    )


async def reconcile_candidate(
    conn: asyncpg.Connection,
    *,
    gw: StripeGateway,
    candidate: ReconcileCandidate,
) -> Dict[str, Any]:
    subscription = await gw.retrieve_subscription(candidate.gateway_subscription_id)
    if not isinstance(subscription, dict) or not subscription.get("id"):
        raise RuntimeError(f"stripe_subscription_not_found:{candidate.gateway_subscription_id}")

    await sync_subscription_and_entitlement(
        conn,
        subscription=subscription,
        latest_invoice_status=None,
    )

    await mark_reconcile_attempt(
        conn,
        gateway_subscription_id=candidate.gateway_subscription_id,
        ok=True,
        message="reconciled",
    )
    return {
        "user_id": str(candidate.user_id),
        "gateway_subscription_id": candidate.gateway_subscription_id,
        "ok": True,
    }


async def run_subscription_reconciler_once(
    pool: asyncpg.Pool,
    *,
    gw: Optional[StripeGateway] = None,
    lookahead_minutes: int = 30,
    limit: int = 100,
) -> Dict[str, Any]:
    gateway = gw or StripeGateway()
    results: List[Dict[str, Any]] = []

    async with pool.acquire() as conn:
        candidates = await list_reconcile_candidates(
            conn,
            lookahead_minutes=lookahead_minutes,
            limit=limit,
        )

    for candidate in candidates:
        async with pool.acquire() as conn:
            try:
                async with conn.transaction():
                    result = await reconcile_candidate(
                        conn,
                        gw=gateway,
                        candidate=candidate,
                    )
                results.append(result)
            except Exception as exc:
                await mark_reconcile_attempt(
                    conn,
                    gateway_subscription_id=candidate.gateway_subscription_id,
                    ok=False,
                    message=str(exc),
                )
                results.append(
                    {
                        "user_id": str(candidate.user_id),
                        "gateway_subscription_id": candidate.gateway_subscription_id,
                        "ok": False,
                        "error": str(exc),
                    }
                )

    return {
        "ok": True,
        "count": len(results),
        "results": results,
    }
