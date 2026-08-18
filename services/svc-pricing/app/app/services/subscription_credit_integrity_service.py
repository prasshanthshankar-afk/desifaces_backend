from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import UUID

import asyncpg

from app.services.entitlements.plan_credit_reconciliation_service import reconcile_included_plan_credits
from desifaces_shared.v3.subscription_cycle import (
    native_cycle_key,
    native_source_ref,
    normalize_subscription_provider,
    stripe_cycle_key,
)


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


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _period_iso(value: Any) -> str | None:
    dt = _as_utc(value)
    return dt.isoformat() if dt is not None else None


def _apple_legacy_cycle_keys(period_start: Any) -> list[str]:
    """Cycle keys used by the existing Apple service-level reconciler."""
    start = _as_utc(period_start)
    if start is None:
        return []
    return [f"{start.year:04d}-{start.month:02d}", f"{start.year:04d}"]


async def _list_active_contexts(conn: asyncpg.Connection, *, limit: int) -> List[Any]:
    return await conn.fetch(
        """
        with ranked as (
          select
            p.user_id,
            p.gateway_provider,
            p.gateway_subscription_id,
            p.plan_code as subscription_plan_code,
            p.current_period_start,
            p.current_period_end,
            p.entitlement_state,
            p.subscription_state,
            p.updated_at,
            row_number() over(
              partition by p.user_id
              order by p.current_period_start desc nulls last, p.updated_at desc, p.created_at desc
            ) rn
          from public.payment_plan_subscriptions p
          where p.gateway_subscription_id is not null
            and lower(coalesce(p.entitlement_state,''))='active'
            and lower(coalesce(p.subscription_state,'')) in ('active','trialing')
            and (p.current_period_end is null or p.current_period_end > now())
        )
        select
          r.user_id,
          r.gateway_provider,
          r.gateway_subscription_id,
          r.subscription_plan_code,
          r.current_period_start,
          r.current_period_end,
          be.plan_code,
          be.tier_code,
          be.included_credits_total,
          public.df_v3_resolve_user_billing_account_id(r.user_id) as billing_account_id
        from ranked r
        join lateral (
          select b.plan_code,b.tier_code,b.included_credits_total
          from public.billing_entitlements b
          where b.user_id=r.user_id
            and (b.effective_from is null or b.effective_from<=now())
            and (b.effective_to is null or b.effective_to>now())
          order by b.updated_at desc nulls last,b.created_at desc nulls last
          limit 1
        ) be on true
        where r.rn=1
          and coalesce(be.included_credits_total,0)>0
        order by r.updated_at asc
        limit $1
        """,
        max(1, min(int(limit), 500)),
    )


async def _audit(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    billing_account_id: UUID,
    provider: str,
    gateway_subscription_id: str,
    cycle_key: str,
    plan_code: str,
    action: str,
    result: Dict[str, Any],
) -> None:
    await conn.execute(
        """
        insert into public.v3_subscription_credit_reconciliation(
          user_id,billing_account_id,gateway_provider,gateway_subscription_id,
          cycle_key,plan_code,action,result_json
        ) values($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
        on conflict(user_id,gateway_provider,gateway_subscription_id,cycle_key,action)
        do update set result_json=excluded.result_json
        """,
        user_id,
        billing_account_id,
        provider,
        gateway_subscription_id,
        cycle_key,
        plan_code,
        action,
        result,
    )


async def repair_active_subscription_credit_cycles(
    conn: asyncpg.Connection,
    *,
    limit: int = 200,
) -> Dict[str, Any]:
    """Repair missed current-period credit reconciliation without inventing periods.

    Only subscription periods already persisted as active are considered. Stripe
    uses the same cycle key as entitlement sync. For Apple/Google, current-period
    evidence can come from either the DB-level ``subscription_cycle:*`` source-ref
    or a service-level plan reconciliation lot whose metadata carries the exact
    persisted period end (period start is the fallback when end is unavailable).

    This period-aware check is important for Google Play: the purchase token and
    original startTime can remain constant while expiryTime advances. A spent lot
    from the previous period therefore does not match the new current_period_end
    and is repaired, while an already-correct current-period lot is never reset.
    """
    rows = await _list_active_contexts(conn, limit=limit)
    results: List[Dict[str, Any]] = []

    for row in rows:
        user_id = UUID(str(_record_get(row, "user_id")))
        billing_account_raw = _record_get(row, "billing_account_id")
        if not billing_account_raw:
            raise RuntimeError(f"subscription_credit_account_context_missing:{user_id}")
        billing_account_id = UUID(str(billing_account_raw))
        provider = normalize_subscription_provider(_record_get(row, "gateway_provider"))
        gateway_subscription_id = str(_record_get(row, "gateway_subscription_id"))
        period_start = _record_get(row, "current_period_start")
        period_end = _record_get(row, "current_period_end")
        plan_code = str(_record_get(row, "plan_code") or _record_get(row, "subscription_plan_code") or "free").strip().lower()
        tier_code = str(_record_get(row, "tier_code") or "free").strip().lower()
        included_cap = int(_record_get(row, "included_credits_total") or 0)

        if provider == "stripe":
            if period_start is None or period_end is None:
                results.append({"user_id": str(user_id), "provider": provider, "action": "skipped_missing_period"})
                continue
            cycle = stripe_cycle_key(gateway_subscription_id, period_start, period_end)
            result = await reconcile_included_plan_credits(
                conn,
                user_id=user_id,
                plan_code=plan_code,
                tier_code=tier_code,
                included_credit_cap=included_cap,
                cycle_key=cycle,
                current_period_start=period_start,
                current_period_end=period_end,
                source="subscription_credit_integrity",
                metadata_json={
                    "provider": provider,
                    "gateway_subscription_id": gateway_subscription_id,
                    "reason": "active_subscription_cycle_integrity",
                },
            )
            action = str(result.get("action") or "reconciled")
            await _audit(
                conn,
                user_id=user_id,
                billing_account_id=billing_account_id,
                provider=provider,
                gateway_subscription_id=gateway_subscription_id,
                cycle_key=cycle,
                plan_code=plan_code,
                action=action,
                result=result,
            )
            results.append({"user_id": str(user_id), "provider": provider, "cycle_key": cycle, "action": action})
            continue

        if provider in {"apple_iap", "google_play"}:
            if period_start is None and period_end is None:
                results.append({"user_id": str(user_id), "provider": provider, "action": "skipped_missing_period"})
                continue

            cycle = native_cycle_key(provider, period_start, period_end)
            expected_ref = native_source_ref(provider, gateway_subscription_id, period_start, period_end)
            legacy_apple_keys = _apple_legacy_cycle_keys(period_start) if provider == "apple_iap" else []
            period_start_iso = _period_iso(period_start)
            period_end_iso = _period_iso(period_end)

            existing = await conn.fetchrow(
                """
                select id, source_ref, metadata_json->>'cycle_key' as cycle_key
                from public.pricing_credit_lots
                where user_id=$1
                  and bucket_type='included'
                  and source_type='plan_grant'
                  and status='active'
                  and (expires_at is null or expires_at>now())
                  and (
                    source_ref=$2
                    or ($3::text[] <> '{}'::text[] and metadata_json->>'cycle_key'=any($3::text[]))
                    or (
                      jsonb_typeof(metadata_json)='object'
                      and $4::text is not null
                      and metadata_json->>'current_period_end'=$4::text
                    )
                    or (
                      jsonb_typeof(metadata_json)='object'
                      and $4::text is null
                      and $5::text is not null
                      and metadata_json->>'current_period_start'=$5::text
                    )
                  )
                order by created_at desc
                limit 1
                """,
                user_id,
                expected_ref,
                legacy_apple_keys,
                period_end_iso,
                period_start_iso,
            )
            if existing:
                result = {
                    "ok": True,
                    "action": "current_cycle_present",
                    "source_ref": _record_get(existing, "source_ref"),
                    "evidence_cycle_key": _record_get(existing, "cycle_key"),
                    "period_start": period_start_iso,
                    "period_end": period_end_iso,
                }
                action = "current_cycle_present"
            else:
                sync_row = await conn.fetchrow(
                    "select public.df_sync_subscription_cycle_credits($1::uuid) as result",
                    user_id,
                )
                raw_result = _record_get(sync_row, "result", {})
                if isinstance(raw_result, str):
                    try:
                        raw_result = json.loads(raw_result)
                    except Exception:
                        raw_result = {"raw": raw_result}
                result = dict(raw_result or {}) if isinstance(raw_result, dict) else {"result": raw_result}
                if result.get("ok") is False:
                    raise RuntimeError(f"native_subscription_cycle_sync_failed:{provider}:{user_id}:{result}")
                action = str(result.get("action") or "native_cycle_repaired")

            await _audit(
                conn,
                user_id=user_id,
                billing_account_id=billing_account_id,
                provider=provider,
                gateway_subscription_id=gateway_subscription_id,
                cycle_key=cycle,
                plan_code=plan_code,
                action=action,
                result=result,
            )
            results.append({"user_id": str(user_id), "provider": provider, "cycle_key": cycle, "action": action})
            continue

        results.append({"user_id": str(user_id), "provider": provider, "action": "skipped_unsupported_provider"})

    return {"ok": True, "count": len(results), "results": results}
