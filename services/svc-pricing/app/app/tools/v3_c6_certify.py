from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.db import ensure_db_pool
from app.services.entitlements.plan_credit_reconciliation_service import reconcile_included_plan_credits
from app.services.subscription_credit_integrity_service import repair_active_subscription_credit_cycles


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _sum_purchased(conn, user_id: UUID) -> tuple[int, int]:
    row = await conn.fetchrow(
        """
        select
          coalesce(sum(remaining_amount),0)::bigint as remaining,
          coalesce(sum(reserved_amount),0)::bigint as reserved
        from public.pricing_credit_lots
        where user_id=$1 and bucket_type='purchased'
        """,
        user_id,
    )
    return int(row["remaining"] or 0), int(row["reserved"] or 0)


async def _cycle_totals(conn, user_id: UUID, cycle_key: str) -> tuple[int, int, int]:
    row = await conn.fetchrow(
        """
        select
          coalesce(sum(granted_amount),0)::bigint as granted,
          coalesce(sum(remaining_amount),0)::bigint as remaining,
          coalesce(sum(reserved_amount),0)::bigint as reserved
        from public.pricing_credit_lots
        where user_id=$1
          and bucket_type='included'
          and status='active'
          and metadata_json->>'cycle_key'=$2
        """,
        user_id,
        cycle_key,
    )
    return int(row["granted"] or 0), int(row["remaining"] or 0), int(row["reserved"] or 0)


async def main() -> None:
    pool = await ensure_db_pool()
    async with pool.acquire() as conn:
        required = [
            "public.v3_subscription_credit_reconciliation",
            "public.pricing_credit_lots",
            "public.pricing_billing_accounts",
        ]
        for name in required:
            if not await conn.fetchval("select to_regclass($1)", name):
                raise RuntimeError(f"C6_CERT_FAIL=required_relation_missing:{name}")
        for fn in (
            "public.df_v3_resolve_user_billing_account_id(uuid)",
            "public.df_sync_subscription_cycle_credits(uuid)",
        ):
            if not await conn.fetchval("select to_regprocedure($1)", fn):
                raise RuntimeError(f"C6_CERT_FAIL=required_function_missing:{fn}")
        print("C6_SCHEMA=PASS")

        missing_accounts = int(
            await conn.fetchval(
                "select count(*) from public.pricing_credit_lots where user_id is not null and billing_account_id is null"
            )
            or 0
        )
        if missing_accounts:
            raise RuntimeError(f"C6_CERT_FAIL=credit_lot_account_context_missing:{missing_accounts}")
        print("C6_ACCOUNT_OWNERSHIP=PASS")

        row = await conn.fetchrow(
            """
            select
              be.user_id,
              public.df_v3_resolve_user_billing_account_id(be.user_id) as account_id,
              lower(coalesce(be.plan_code,'free')) as plan_code,
              lower(coalesce(be.tier_code,'free')) as tier_code,
              greatest(coalesce(be.included_credits_total,0),100)::int as included_cap
            from public.billing_entitlements be
            where (be.effective_from is null or be.effective_from<=now())
              and (be.effective_to is null or be.effective_to>now())
              and public.df_v3_resolve_user_billing_account_id(be.user_id) is not null
            order by coalesce(be.included_credits_total,0) desc, be.updated_at desc nulls last
            limit 1
            """
        )
        if not row:
            raise RuntimeError("C6_CERT_FAIL=no_user_entitlement_account_context")

        user_id = UUID(str(row["user_id"]))
        account_id = UUID(str(row["account_id"]))
        plan_code = str(row["plan_code"] or "free")
        tier_code = str(row["tier_code"] or "free")
        cap = int(row["included_cap"] or 100)
        cap = max(100, cap)

        before_purchased = await _sum_purchased(conn, user_id)
        baseline_reconcile_rows = int(await conn.fetchval("select count(*) from public.v3_subscription_credit_reconciliation") or 0)

        tx = conn.transaction()
        await tx.start()
        try:
            nonce = uuid4().hex[:10]
            cycle1 = f"v3-c6-cert-{nonce}-month-1"
            cycle2 = f"v3-c6-cert-{nonce}-month-2"
            start1 = _now().replace(microsecond=0)
            end1 = start1 + timedelta(days=30)
            start2 = end1
            end2 = start2 + timedelta(days=30)

            result1 = await reconcile_included_plan_credits(
                conn,
                user_id=user_id,
                plan_code=plan_code,
                tier_code=tier_code,
                included_credit_cap=cap,
                cycle_key=cycle1,
                current_period_start=start1,
                current_period_end=end1,
                source="v3_c6_certification",
                metadata_json={
                    "certification": "V3-C6",
                    "billing_account_id": str(account_id),
                    "cycle": 1,
                    "nonce": nonce,
                },
            )
            granted1, remaining1, reserved1 = await _cycle_totals(conn, user_id, cycle1)
            if granted1 < cap or remaining1 + reserved1 < cap:
                raise RuntimeError(
                    f"C6_CERT_FAIL=cycle1_not_funded:cap={cap}:granted={granted1}:remaining={remaining1}:reserved={reserved1}:result={result1}"
                )

            # Simulate committed usage within cycle 1 by spending an unreserved
            # portion of the included lot. Purchased/top-up lots are untouched.
            spend = max(1, min(100, cap // 2))
            await conn.execute(
                """
                with target as (
                  select id
                  from public.pricing_credit_lots
                  where user_id=$1
                    and bucket_type='included'
                    and status='active'
                    and metadata_json->>'cycle_key'=$2
                    and remaining_amount-reserved_amount >= $3
                  order by created_at desc
                  limit 1
                  for update
                )
                update public.pricing_credit_lots l
                set remaining_amount=remaining_amount-$3, updated_at=now()
                from target t
                where l.id=t.id
                """,
                user_id,
                cycle1,
                spend,
            )

            result2 = await reconcile_included_plan_credits(
                conn,
                user_id=user_id,
                plan_code=plan_code,
                tier_code=tier_code,
                included_credit_cap=cap,
                cycle_key=cycle2,
                current_period_start=start2,
                current_period_end=end2,
                source="v3_c6_certification",
                metadata_json={
                    "certification": "V3-C6",
                    "billing_account_id": str(account_id),
                    "cycle": 2,
                    "nonce": nonce,
                },
            )
            granted2, remaining2, reserved2 = await _cycle_totals(conn, user_id, cycle2)
            if granted2 != cap or remaining2 + reserved2 != cap:
                raise RuntimeError(
                    f"C6_CERT_FAIL=renewal_not_replenished:cap={cap}:granted={granted2}:remaining={remaining2}:reserved={reserved2}:result={result2}"
                )
            print("C6_MONTHLY_RENEWAL_REPLENISHMENT=PASS")

            before_duplicate = await _cycle_totals(conn, user_id, cycle2)
            duplicate = await reconcile_included_plan_credits(
                conn,
                user_id=user_id,
                plan_code=plan_code,
                tier_code=tier_code,
                included_credit_cap=cap,
                cycle_key=cycle2,
                current_period_start=start2,
                current_period_end=end2,
                source="v3_c6_certification",
                metadata_json={
                    "certification": "V3-C6",
                    "billing_account_id": str(account_id),
                    "cycle": 2,
                    "nonce": nonce,
                },
            )
            after_duplicate = await _cycle_totals(conn, user_id, cycle2)
            if before_duplicate != after_duplicate:
                raise RuntimeError(
                    f"C6_CERT_FAIL=duplicate_cycle_double_grant:before={before_duplicate}:after={after_duplicate}:result={duplicate}"
                )
            print("C6_RENEWAL_IDEMPOTENCY=PASS")

            during_purchased = await _sum_purchased(conn, user_id)
            if during_purchased != before_purchased:
                raise RuntimeError(
                    f"C6_CERT_FAIL=purchased_topup_changed:before={before_purchased}:after={during_purchased}"
                )
            print("C6_TOPUP_PRESERVATION=PASS")

            # Run the provider-neutral integrity sweep against the cloned active
            # subscription state inside the same rollback transaction. It can
            # repair DB state but never invokes a provider API and never advances
            # a provider billing period.
            integrity = await repair_active_subscription_credit_cycles(conn, limit=200)
            if not bool(integrity.get("ok")):
                raise RuntimeError(f"C6_CERT_FAIL=integrity_sweep_failed:{integrity}")
            print("C6_ACTIVE_PERIOD_INTEGRITY_SWEEP=PASS")
        finally:
            await tx.rollback()

        after_purchased = await _sum_purchased(conn, user_id)
        after_reconcile_rows = int(await conn.fetchval("select count(*) from public.v3_subscription_credit_reconciliation") or 0)
        if after_purchased != before_purchased or after_reconcile_rows != baseline_reconcile_rows:
            raise RuntimeError(
                f"C6_CERT_FAIL=rollback_failed:purchased_before={before_purchased}:purchased_after={after_purchased}:audit_before={baseline_reconcile_rows}:audit_after={after_reconcile_rows}"
            )
        print("C6_CERTIFICATION_ROLLBACK=PASS")
        print("V3_C6_RUNTIME_CERTIFICATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
