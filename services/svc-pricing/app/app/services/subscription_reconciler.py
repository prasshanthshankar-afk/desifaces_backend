from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.services.gateways.stripe_gateway import StripeGateway
from app.services.entitlement_sync_service import sync_subscription_and_entitlement
from app.repo.google_play_iap_repo import revert_user_to_free_entitlement


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
    gateway_provider: str
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
            p.gateway_provider,
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
          gateway_provider,
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
                gateway_provider=str(row.get("gateway_provider") or "").strip().lower(),
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


def _is_stale_period(candidate: ReconcileCandidate) -> bool:
    if candidate.current_period_end is None:
        return True
    end = candidate.current_period_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end <= datetime.now(timezone.utc)


async def reconcile_google_play_candidate(
    conn: asyncpg.Connection,
    *,
    candidate: ReconcileCandidate,
) -> Dict[str, Any]:
    """Launch-safe Google Play fallback reconciliation.

    The scheduled reconciler does not have the raw Google purchase token; only
    app restore/confirm and Google RTDN carry that token. Therefore this worker
    must not invent a renewal and must not try Stripe APIs. If a Google period is
    stale and no RTDN/restore has advanced it, suspend the provider entitlement
    and expire included credits while preserving purchased/top-up wallet lots.
    A subsequent app restore/confirm with a valid Google token will reactivate
    and sync the current cycle.
    """
    if not _is_stale_period(candidate):
        await mark_reconcile_attempt(
            conn,
            gateway_subscription_id=candidate.gateway_subscription_id,
            ok=True,
            message="google_play_current_period_not_stale",
        )
        return {
            "user_id": str(candidate.user_id),
            "gateway_provider": candidate.gateway_provider,
            "gateway_subscription_id": candidate.gateway_subscription_id,
            "ok": True,
            "action": "noop_current_period_not_stale",
        }

    metadata = {
        "provider": "google_play",
        "source": "subscription_reconciler",
        "reconciler_google_play_action": "stale_period_suspended_pending_rtdn_or_restore",
        "reconciler_previous_period_end": candidate.current_period_end.isoformat() if candidate.current_period_end else None,
        "reconciler_requires": "google_rtdn_or_app_restore_confirm",
    }

    await conn.execute(
        """
        update payment_plan_subscriptions
        set subscription_state = 'canceled',
            entitlement_state = 'inactive',
            canceled_at = coalesce(canceled_at, now()),
            metadata_json = (
              case
                when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
                  then coalesce(metadata_json, '{}'::jsonb)
                else '{}'::jsonb
              end
            ) || jsonb_build_object(
                   'reconciler_google_play_action', 'stale_period_suspended_pending_rtdn_or_restore',
                   'reconciler_provider', 'google_play',
                   'reconciler_previous_period_end', current_period_end,
                   'reconciler_requires', 'google_rtdn_or_app_restore_confirm',
                   'reconciler_last_run_at', now(),
                   'reconciler_last_status', 'ok',
                   'reconciler_last_message', 'google_play_stale_period_suspended_pending_rtdn_or_restore'
                 ),
            updated_at = now()
        where gateway_subscription_id = $1
          and gateway_provider = 'google_play'
          and (current_period_end is null or current_period_end <= now())
        """,
        candidate.gateway_subscription_id,
    )

    # The provider subscription is no longer active, so the canonical billing
    # entitlement must also return to Free. Purchased/top-up wallet lots are
    # intentionally preserved by the entitlement/lot reconciliation functions.
    await revert_user_to_free_entitlement(
        conn,
        user_id=candidate.user_id,
        source="google_play_reconciler_stale_period",
        metadata_json=metadata,
    )

    # Expire stale paid-plan included lots and recompute cached credit account.
    # This DB function preserves purchased/top-up lots.
    await conn.fetchrow(
        "select public.df_sync_subscription_cycle_credits($1::uuid) as sync_result",
        candidate.user_id,
    )

    return {
        "user_id": str(candidate.user_id),
        "gateway_provider": candidate.gateway_provider,
        "gateway_subscription_id": candidate.gateway_subscription_id,
        "ok": True,
        "action": "google_play_stale_period_suspended_pending_rtdn_or_restore",
    }


async def reconcile_google_play_orphan_entitlements(
    conn: asyncpg.Connection,
    *,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Repair canonical Google Play entitlements with no active provider row.

    This covers the case where a stale Google provider subscription was already
    marked canceled/inactive before a later reconciler version learned to revert
    billing_entitlements. It is intentionally Google-only and preserves purchased
    wallet/top-up lots through the existing entitlement and lot reconciliation
    helpers.
    """
    rows = await conn.fetch(
        """
        select
          be.user_id,
          be.plan_code,
          be.tier_code,
          be.source,
          be.effective_from,
          be.updated_at
        from public.billing_entitlements be
        where lower(coalesce(be.source, '')) = 'google_play'
          and lower(coalesce(be.plan_code, '')) not in ('', 'free')
          and lower(coalesce(be.billing_mode, '')) in ('subscription', 'credits', 'prepaid')
          and (be.effective_from is null or be.effective_from <= now())
          and (be.effective_to is null or be.effective_to > now())
          and not exists (
            select 1
            from public.payment_plan_subscriptions p
            where p.user_id = be.user_id
              and lower(coalesce(p.gateway_provider, '')) = 'google_play'
              and lower(coalesce(p.entitlement_state, '')) in ('active', 'grace')
              and lower(coalesce(p.subscription_state, '')) in ('active', 'trialing', 'past_due', 'unpaid', 'paused')
              and (p.current_period_end is null or p.current_period_end > now())
          )
        order by be.updated_at asc nulls first
        limit $1
        """,
        limit,
    )

    results: List[Dict[str, Any]] = []
    for row in rows:
        user_id = row["user_id"]
        metadata = {
            "provider": "google_play",
            "source": "subscription_reconciler",
            "reconciler_google_play_action": "orphan_entitlement_reverted_to_free",
            "reconciler_previous_plan_code": str(row.get("plan_code") or ""),
            "reconciler_reason": "google_play_billing_entitlement_without_active_provider_subscription",
        }

        await revert_user_to_free_entitlement(
            conn,
            user_id=user_id,
            source="google_play_reconciler_orphan_entitlement",
            metadata_json=metadata,
        )

        await conn.fetchrow(
            "select public.df_sync_subscription_cycle_credits($1::uuid) as sync_result",
            user_id,
        )

        await conn.execute(
            """
            update public.payment_plan_subscriptions
            set metadata_json = (
                  case
                    when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
                      then coalesce(metadata_json, '{}'::jsonb)
                    else '{}'::jsonb
                  end
                ) || jsonb_build_object(
                   'reconciler_google_play_action', 'orphan_entitlement_reverted_to_free',
                   'reconciler_provider', 'google_play',
                   'reconciler_last_run_at', now(),
                   'reconciler_last_status', 'ok',
                   'reconciler_last_message', 'google_play_orphan_entitlement_reverted_to_free'
                 ),
                updated_at = now()
            where user_id = $1
              and lower(coalesce(gateway_provider, '')) = 'google_play'
              and lower(coalesce(entitlement_state, '')) = 'inactive'
            """,
            user_id,
        )

        results.append(
            {
                "user_id": str(user_id),
                "gateway_provider": "google_play",
                "ok": True,
                "action": "google_play_orphan_entitlement_reverted_to_free",
                "previous_plan_code": str(row.get("plan_code") or ""),
            }
        )

    return results


async def reconcile_candidate(
    conn: asyncpg.Connection,
    *,
    gw: StripeGateway,
    candidate: ReconcileCandidate,
) -> Dict[str, Any]:
    if candidate.gateway_provider == "google_play":
        return await reconcile_google_play_candidate(conn, candidate=candidate)
    if candidate.gateway_provider != "stripe":
        await mark_reconcile_attempt(
            conn,
            gateway_subscription_id=candidate.gateway_subscription_id,
            ok=True,
            message=f"reconciler_skipped_unsupported_provider:{candidate.gateway_provider}",
        )
        return {
            "user_id": str(candidate.user_id),
            "gateway_provider": candidate.gateway_provider,
            "gateway_subscription_id": candidate.gateway_subscription_id,
            "ok": True,
            "action": "skipped_unsupported_provider",
        }

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
        "gateway_provider": candidate.gateway_provider,
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
                        "gateway_provider": candidate.gateway_provider,
                        "gateway_subscription_id": candidate.gateway_subscription_id,
                        "ok": False,
                        "error": str(exc),
                    }
                )

    orphan_results: List[Dict[str, Any]] = []
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                orphan_results = await reconcile_google_play_orphan_entitlements(
                    conn,
                    limit=limit,
                )
        except Exception as exc:
            orphan_results = [
                {
                    "ok": False,
                    "gateway_provider": "google_play",
                    "action": "google_play_orphan_entitlement_repair_failed",
                    "error": str(exc),
                }
            ]

    all_results = results + orphan_results
    return {
        "ok": True,
        "count": len(all_results),
        "results": all_results,
    }
