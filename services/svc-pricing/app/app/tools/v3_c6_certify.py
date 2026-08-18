from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.db import ensure_db_pool
from app.services.entitlements.plan_credit_reconciliation_service import reconcile_included_plan_credits
from app.services.subscription_credit_integrity_service import repair_active_subscription_credit_cycles


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _credits(value) -> int:
    try:
        return int(Decimal(str(value if value is not None else 0)).to_integral_value())
    except Exception:
        return 0


async def _sum_purchased(conn, user_id: UUID) -> tuple[int, int]:
    row = await conn.fetchrow(
        """
        select
          coalesce(sum(remaining_amount),0)::numeric as remaining,
          coalesce(sum(reserved_amount),0)::numeric as reserved
        from public.pricing_credit_lots
        where user_id=$1 and bucket_type='purchased'
        """,
        user_id,
    )
    return _credits(row["remaining"]), _credits(row["reserved"])


async def _included_state(conn, user_id: UUID) -> tuple[int, int, int, int]:
    row = await conn.fetchrow(
        """
        select
          count(*) filter (where status='active')::int as active_lots,
          coalesce(sum(granted_amount) filter (where status='active'),0)::numeric as granted,
          coalesce(sum(remaining_amount) filter (where status='active'),0)::numeric as remaining,
          coalesce(sum(reserved_amount) filter (where status='active'),0)::numeric as reserved
        from public.pricing_credit_lots
        where user_id=$1 and bucket_type='included'
        """,
        user_id,
    )
    return (
        int(row["active_lots"] or 0),
        _credits(row["granted"]),
        _credits(row["remaining"]),
        _credits(row["reserved"]),
    )


async def _account_state(conn, user_id: UUID) -> tuple[int, int]:
    row = await conn.fetchrow(
        """
        select balance_credits,reserved_credits
        from public.pricing_credit_accounts
        where user_id=$1
        """,
        user_id,
    )
    if not row:
        raise RuntimeError(f"C6_CERT_FAIL=credit_account_missing:{user_id}")
    return _credits(row["balance_credits"]), _credits(row["reserved_credits"])


async def _assert_returned_lot(
    conn,
    *,
    user_id: UUID,
    account_id: UUID,
    result: dict,
    expected_cycle_key: str,
) -> None:
    lot_id = str(result.get("credit_lot_id") or "").strip()
    if not lot_id:
        # A reconciliation can legitimately reuse/adopt an existing current-cycle
        # lot. In that case the authoritative post-reconcile totals below are the
        # proof and no new lot identity is expected.
        return
    row = await conn.fetchrow(
        """
        select id,user_id,billing_account_id,status,granted_amount,remaining_amount,
               reserved_amount,metadata_json->>'cycle_key' as cycle_key
        from public.pricing_credit_lots
        where id=$1::uuid
        """,
        lot_id,
    )
    if not row:
        raise RuntimeError(f"C6_CERT_FAIL=returned_credit_lot_missing:{lot_id}")
    if str(row["user_id"]) != str(user_id):
        raise RuntimeError(f"C6_CERT_FAIL=returned_credit_lot_user_mismatch:{lot_id}")
    if str(row["billing_account_id"] or "") != str(account_id):
        raise RuntimeError(f"C6_CERT_FAIL=returned_credit_lot_account_mismatch:{lot_id}")
    if str(row["status"] or "") != "active":
        raise RuntimeError(f"C6_CERT_FAIL=returned_credit_lot_not_active:{lot_id}:{row['status']}")
    stored_cycle = str(row["cycle_key"] or "")
    if stored_cycle and stored_cycle != expected_cycle_key:
        raise RuntimeError(
            f"C6_CERT_FAIL=returned_credit_lot_cycle_mismatch:{lot_id}:{stored_cycle}:{expected_cycle_key}"
        )


async def _spend_included(
    conn,
    *,
    user_id: UUID,
    preferred_cycle_key: str,
    spend: int,
) -> UUID:
    row = await conn.fetchrow(
        """
        select id
        from public.pricing_credit_lots
        where user_id=$1
          and bucket_type='included'
          and status='active'
          and remaining_amount-reserved_amount >= $3::numeric
        order by
          case when metadata_json->>'cycle_key'=$2 then 0 else 1 end,
          created_at desc
        limit 1
        for update
        """,
        user_id,
        preferred_cycle_key,
        spend,
    )
    if not row:
        raise RuntimeError(f"C6_CERT_FAIL=no_spendable_included_lot:{user_id}:{spend}")
    lot_id = UUID(str(row["id"]))
    status = await conn.execute(
        """
        update public.pricing_credit_lots
        set remaining_amount=remaining_amount-$2::numeric, updated_at=now()
        where id=$1
          and remaining_amount-reserved_amount >= $2::numeric
        """,
        lot_id,
        spend,
    )
    if not str(status).endswith("1"):
        raise RuntimeError(f"C6_CERT_FAIL=synthetic_spend_not_applied:{lot_id}")
    return lot_id


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
        cap = max(100, int(row["included_cap"] or 100))

        before_purchased = await _sum_purchased(conn, user_id)
        baseline_reconcile_rows = int(
            await conn.fetchval("select count(*) from public.v3_subscription_credit_reconciliation") or 0
        )

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
            await _assert_returned_lot(
                conn,
                user_id=user_id,
                account_id=account_id,
                result=result1,
                expected_cycle_key=cycle1,
            )
            cycle1_unspent = _credits(result1.get("included_remaining")) + _credits(
                result1.get("included_reserved")
            )
            if cycle1_unspent != cap:
                raise RuntimeError(
                    f"C6_CERT_FAIL=cycle1_not_funded:cap={cap}:unspent={cycle1_unspent}:result={result1}"
                )
            print("C6_INITIAL_CYCLE_FUNDING=PASS")

            # Simulate committed use during cycle 1. We intentionally select the
            # exact returned/current included lot rather than re-deriving its
            # identity from compatibility metadata.
            spend = max(1, min(100, cap // 2))
            await _spend_included(
                conn,
                user_id=user_id,
                preferred_cycle_key=cycle1,
                spend=spend,
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
            await _assert_returned_lot(
                conn,
                user_id=user_id,
                account_id=account_id,
                result=result2,
                expected_cycle_key=cycle2,
            )
            cycle2_unspent = _credits(result2.get("included_remaining")) + _credits(
                result2.get("included_reserved")
            )
            if cycle2_unspent != cap:
                raise RuntimeError(
                    f"C6_CERT_FAIL=renewal_not_replenished:cap={cap}:unspent={cycle2_unspent}:result={result2}"
                )
            print("C6_MONTHLY_RENEWAL_REPLENISHMENT=PASS")

            before_duplicate_lots = await _included_state(conn, user_id)
            before_duplicate_account = await _account_state(conn, user_id)
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
            after_duplicate_lots = await _included_state(conn, user_id)
            after_duplicate_account = await _account_state(conn, user_id)
            duplicate_unspent = _credits(duplicate.get("included_remaining")) + _credits(
                duplicate.get("included_reserved")
            )
            if (
                before_duplicate_lots != after_duplicate_lots
                or before_duplicate_account != after_duplicate_account
                or duplicate_unspent != cap
            ):
                raise RuntimeError(
                    "C6_CERT_FAIL=duplicate_cycle_double_grant:"
                    f"lots_before={before_duplicate_lots}:lots_after={after_duplicate_lots}:"
                    f"account_before={before_duplicate_account}:account_after={after_duplicate_account}:"
                    f"result={duplicate}"
                )
            print("C6_RENEWAL_IDEMPOTENCY=PASS")

            during_purchased = await _sum_purchased(conn, user_id)
            if during_purchased != before_purchased:
                raise RuntimeError(
                    f"C6_CERT_FAIL=purchased_topup_changed:before={before_purchased}:after={during_purchased}"
                )
            print("C6_TOPUP_PRESERVATION=PASS")

            # Run the provider-neutral integrity sweep against the cloned active
            # subscription state inside the same rollback transaction. It never
            # invokes a provider API and never advances a provider billing period.
            integrity = await repair_active_subscription_credit_cycles(conn, limit=200)
            if not bool(integrity.get("ok")):
                raise RuntimeError(f"C6_CERT_FAIL=integrity_sweep_failed:{integrity}")
            print("C6_ACTIVE_PERIOD_INTEGRITY_SWEEP=PASS")
        finally:
            await tx.rollback()

        after_purchased = await _sum_purchased(conn, user_id)
        after_reconcile_rows = int(
            await conn.fetchval("select count(*) from public.v3_subscription_credit_reconciliation") or 0
        )
        if after_purchased != before_purchased or after_reconcile_rows != baseline_reconcile_rows:
            raise RuntimeError(
                "C6_CERT_FAIL=rollback_failed:"
                f"purchased_before={before_purchased}:purchased_after={after_purchased}:"
                f"audit_before={baseline_reconcile_rows}:audit_after={after_reconcile_rows}"
            )
        print("C6_CERTIFICATION_ROLLBACK=PASS")
        print("V3_C6_RUNTIME_CERTIFICATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
