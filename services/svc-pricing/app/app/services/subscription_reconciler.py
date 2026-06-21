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
from app.services.entitlements.plan_credit_reconciliation_service import reconcile_included_plan_credits


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



def _json_default_safe(value: Any):
    """JSON serializer fallback for metadata objects.

    Reconciler metadata can contain datetimes, UUIDs, Decimals, asyncpg records,
    or nested service-return payloads. Store metadata as JSON-safe primitives.
    """
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, (set, tuple)):
        return list(value)
    try:
        return str(value)
    except Exception:
        return repr(value)


def _json_dumps_safe(value: Any) -> str:
    return json.dumps(value, default=_json_default_safe)

def _is_stale_period(candidate: ReconcileCandidate) -> bool:
    if candidate.current_period_end is None:
        return True
    end = candidate.current_period_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end <= datetime.now(timezone.utc)




async def _restore_free_included_credit_floor(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    source: str,
    cycle_key: str,
    metadata_json: Dict[str, Any],
) -> Dict[str, Any]:
    """Guarantee a reverted Free user has the Free included-credit contract.

    This is intentionally scoped to included credits only. Purchased/top-up lots
    are preserved and included in the rebuilt legacy aggregate account.
    """
    row = await conn.fetchrow(
        """
        select
          included_credits_total,
          included_credits_remaining
        from billing_entitlements
        where user_id = $1
          and lower(coalesce(tier_code, '')) = 'free'
          and lower(coalesce(plan_code, '')) = 'free'
          and lower(coalesce(billing_mode, '')) = 'free'
          and (effective_from is null or effective_from <= now())
          and (effective_to is null or effective_to > now())
        order by effective_from desc nulls last, updated_at desc nulls last
        limit 1
        """,
        user_id,
    )

    try:
        free_cap = int(row.get("included_credits_total") or 100) if row else 100
    except Exception:
        free_cap = 100
    if free_cap <= 0:
        free_cap = 100

    # Make expired included lots physically non-active so account rebuilds and
    # dashboard views do not accidentally see stale included credits.
    await conn.execute(
        """
        update public.pricing_credit_lots
        set status = 'expired',
            updated_at = now()
        where user_id = $1
          and bucket_type = 'included'
          and status = 'active'
          and expires_at is not null
          and expires_at <= now()
        """,
        user_id,
    )

    # Reuse an active Free lot if present; otherwise create one. If a prior
    # reconciler left an active Free lot with zero remaining, repair it to the
    # Free contract floor.
    lot_row = await conn.fetchrow(
        """
        select id
        from public.pricing_credit_lots
        where user_id = $1
          and bucket_type = 'included'
          and source_type = 'plan_grant'
          and lower(coalesce(plan_code_at_grant, '')) = 'free'
          and status = 'active'
          and (expires_at is null or expires_at > now())
        order by created_at desc nulls last
        limit 1
        """,
        user_id,
    )

    if lot_row:
        lot_id = lot_row["id"]
        await conn.execute(
            """
            update public.pricing_credit_lots
            set granted_amount = greatest(coalesce(granted_amount, 0), $2::numeric),
                remaining_amount = greatest(coalesce(remaining_amount, 0), $2::numeric),
                reserved_amount = least(coalesce(reserved_amount, 0), $2::numeric),
                expires_at = null,
                status = 'active',
                metadata_json = case
                    when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
                    then coalesce(metadata_json, '{}'::jsonb)
                    else '{}'::jsonb
                end || $3::jsonb,
                updated_at = now()
            where id = $1
            """,
            lot_id,
            free_cap,
            _json_dumps_safe(
                {
                    "source": source,
                    "cycle_key": cycle_key,
                    "reason": "restore_free_included_credit_floor",
                    "plan_code": "free",
                    "tier_code": "free",
                    "included_credit_cap": free_cap,
                    **metadata_json,
                }
            ),
        )
    else:
        lot_id = await conn.fetchval(
            """
            insert into public.pricing_credit_lots(
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
            values(
              gen_random_uuid(),
              null,
              $1,
              'included',
              'plan_grant',
              $2,
              'free',
              $3::numeric,
              $3::numeric,
              0,
              now(),
              null,
              'active',
              $4::jsonb,
              now(),
              now()
            )
            returning id
            """,
            user_id,
            f"{source}:free_restore:{cycle_key}",
            free_cap,
            _json_dumps_safe(
                {
                    "source": source,
                    "cycle_key": cycle_key,
                    "reason": "restore_free_included_credit_floor",
                    "plan_code": "free",
                    "tier_code": "free",
                    "included_credit_cap": free_cap,
                    **metadata_json,
                }
            ),
        )

    await conn.execute(
        """
        update public.billing_entitlements
        set tier_code = 'free',
            plan_code = 'free',
            billing_mode = 'free',
            settlement_mode = 'credits',
            included_credits_total = $2::numeric,
            included_credits_remaining = $2::numeric,
            overage_allowed = false,
            wallet_topup_allowed = true,
            hard_stop_on_insufficient_balance = true,
            source = $3,
            metadata_json = case
                when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
                then coalesce(metadata_json, '{}'::jsonb)
                else '{}'::jsonb
            end || $4::jsonb,
            updated_at = now()
        where user_id = $1
          and (effective_from is null or effective_from <= now())
          and (effective_to is null or effective_to > now())
        """,
        user_id,
        free_cap,
        source,
        _json_dumps_safe(
            {
                "source": source,
                "cycle_key": cycle_key,
                "last_free_credit_floor_restore_at": datetime.now(timezone.utc).isoformat(),
                "last_free_credit_floor_lot_id": str(lot_id),
                "included_credit_cap": free_cap,
            }
        ),
    )

    totals = await conn.fetchrow(
        """
        select
          coalesce(sum(
            case
              when status = 'active'
               and (expires_at is null or expires_at > now())
              then coalesce(remaining_amount, 0)
              else 0
            end
          ), 0)::bigint as balance_credits,
          coalesce(sum(
            case
              when status = 'active'
               and (expires_at is null or expires_at > now())
              then coalesce(reserved_amount, 0)
              else 0
            end
          ), 0)::bigint as reserved_credits
        from public.pricing_credit_lots
        where user_id = $1
        """,
        user_id,
    )
    balance_credits = int(totals.get("balance_credits") or 0) if totals else free_cap
    reserved_credits = int(totals.get("reserved_credits") or 0) if totals else 0

    await conn.execute(
        """
        insert into public.pricing_credit_accounts(
          user_id,
          balance_credits,
          reserved_credits,
          settlement_mode,
          updated_at
        )
        values($1, $2, $3, 'prepaid', now())
        on conflict (user_id)
        do update set
          balance_credits = excluded.balance_credits,
          reserved_credits = excluded.reserved_credits,
          settlement_mode = case
              when coalesce(trim(public.pricing_credit_accounts.settlement_mode), '') in ('', 'credits')
              then 'prepaid'
              else public.pricing_credit_accounts.settlement_mode
          end,
          updated_at = now()
        """,
        user_id,
        balance_credits,
        reserved_credits,
    )

    return {
        "action": "free_included_credit_floor_restored",
        "user_id": str(user_id),
        "free_cap": free_cap,
        "lot_id": str(lot_id),
        "balance_credits": balance_credits,
        "reserved_credits": reserved_credits,
    }



async def _reconcile_user_to_free_included_credits(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    source: str,
    cycle_key: str,
    current_period_end: Optional[datetime],
    metadata_json: Dict[str, Any],
) -> Dict[str, Any]:
    """Reconcile included credits after a provider subscription is no longer active.

    Purchased/top-up wallet lots must be preserved. Included paid-plan lots must
    not leak after a Google Play test subscription expires or a real subscription
    becomes stale/canceled.
    """
    row = await conn.fetchrow(
        """
        select
          plan_code,
          tier_code,
          included_credits_total,
          included_credits_remaining
        from billing_entitlements
        where user_id = $1
          and (effective_from is null or effective_from <= now())
          and (effective_to is null or effective_to > now())
        order by effective_from desc nulls last, updated_at desc nulls last
        limit 1
        """,
        user_id,
    )

    plan_code = str(row.get("plan_code") or "free").strip().lower() if row else "free"
    tier_code = str(row.get("tier_code") or "free").strip().lower() if row else "free"

    try:
        included_cap = int(row.get("included_credits_total") or 100) if row else 100
    except Exception:
        included_cap = 100

    if plan_code != "free":
        plan_code = "free"
    if tier_code != "free":
        tier_code = "free"
    if included_cap <= 0:
        included_cap = 100

    reconcile_result = await reconcile_included_plan_credits(
        conn,
        user_id=user_id,
        plan_code=plan_code,
        tier_code=tier_code,
        included_credit_cap=included_cap,
        cycle_key=cycle_key,
        current_period_start=None,
        current_period_end=current_period_end,
        source=source,
        metadata_json={
            **metadata_json,
            "source": source,
            "plan_code": plan_code,
            "tier_code": tier_code,
            "included_credit_cap": included_cap,
            "reason": "provider_subscription_inactive_reconcile_to_free",
        },
    )

    free_floor_result = await _restore_free_included_credit_floor(
        conn,
        user_id=user_id,
        source=source,
        cycle_key=cycle_key,
        metadata_json={
            **metadata_json,
            "reconcile_result": reconcile_result,
        },
    )

    return {
        "action": "reconciled_to_free_included_credits",
        "reconcile_result": reconcile_result,
        "free_floor_result": free_floor_result,
    }


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

    # Expire stale paid-plan included lots and rebuild cached credit display
    # from canonical credit lots. Purchased/top-up lots are preserved.
    await _reconcile_user_to_free_included_credits(
        conn,
        user_id=candidate.user_id,
        source="google_play_reconciler_stale_period",
        cycle_key=(
            "google_play_stale_period:"
            f"{candidate.gateway_subscription_id}:"
            f"{candidate.current_period_end.isoformat() if candidate.current_period_end else 'no_period_end'}"
        ),
        current_period_end=candidate.current_period_end,
        metadata_json={
            **metadata,
            "gateway_subscription_id": candidate.gateway_subscription_id,
        },
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

        await _reconcile_user_to_free_included_credits(
            conn,
            user_id=user_id,
            source="google_play_reconciler_orphan_entitlement",
            cycle_key=f"google_play_orphan_entitlement:{user_id}",
            current_period_end=None,
            metadata_json=metadata,
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
