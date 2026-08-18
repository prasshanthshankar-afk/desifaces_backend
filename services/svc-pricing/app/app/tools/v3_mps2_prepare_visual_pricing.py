from __future__ import annotations

import asyncio
import os
from decimal import Decimal

from app.db import ensure_db_pool
from app.services.subscription_credit_integrity_service import repair_active_subscription_credit_cycles


REQUIRED_CREDITS = Decimal("10")
TEST_USER_EMAIL = str(
    os.getenv("DF_V3_E2E_TEST_USER_EMAIL") or "test_apple_iap_test1@desifaces.ai"
).strip().lower()


async def main() -> None:
    if not TEST_USER_EMAIL:
        raise RuntimeError("MPS2_PRICING_PREP_FAIL=test_user_email_missing")

    pool = await ensure_db_pool()
    async with pool.acquire() as conn:
        # This is not a synthetic credit bypass. Reuse the certified C6 integrity
        # repair so only already-persisted active subscription periods are brought
        # to their authoritative included-credit state.
        async with conn.transaction():
            result = await repair_active_subscription_credit_cycles(conn, limit=200)
        if not bool(result.get("ok")):
            raise RuntimeError(f"MPS2_PRICING_PREP_FAIL=subscription_integrity:{result}")
        print(f"MPS2_PRICING_INTEGRITY_REPAIR=PASS:contexts={int(result.get('count') or 0)}")

        row = await conn.fetchrow(
            """
            with spendable as (
              select
                user_id,
                coalesce(sum(greatest(remaining_amount-reserved_amount,0)) filter (
                  where status='active' and (expires_at is null or expires_at>now())
                ),0)::numeric as available_credits
              from public.pricing_credit_lots
              group by user_id
            )
            select
              bam.user_id,
              bam.billing_account_id,
              coalesce(s.available_credits,0)::numeric as available_credits,
              lower(coalesce(be.tier_code,'free')) as tier_code,
              lower(coalesce(be.plan_code,'free')) as plan_code,
              lower(u.email) as email
            from public.pricing_billing_account_members bam
            join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
            join core.users u on u.id=bam.user_id
            left join spendable s on s.user_id=bam.user_id
            left join lateral (
              select tier_code,plan_code
              from public.billing_entitlements b
              where b.user_id=bam.user_id
                and (b.effective_from is null or b.effective_from<=now())
                and (b.effective_to is null or b.effective_to>now())
              order by b.updated_at desc nulls last,b.created_at desc nulls last
              limit 1
            ) be on true
            where bam.status='active'
              and ba.status='active'
              and lower(u.email)=lower($1::text)
            order by bam.is_default desc,
                     case bam.role when 'owner' then 0 when 'finance_admin' then 1 else 2 end,
                     bam.created_at asc
            limit 1
            """,
            TEST_USER_EMAIL,
        )
        if not row:
            raise RuntimeError(
                f"MPS2_PRICING_PREP_FAIL=canonical_test_actor_not_found:{TEST_USER_EMAIL}"
            )

        available = Decimal(str(row["available_credits"] or 0))
        if available < REQUIRED_CREDITS:
            raise RuntimeError(
                "MPS2_PRICING_PREP_FAIL=canonical_test_actor_underfunded:"
                f"required={REQUIRED_CREDITS}:available={available}"
            )
        if str(row["email"] or "").strip().lower() != TEST_USER_EMAIL:
            raise RuntimeError("MPS2_PRICING_PREP_FAIL=canonical_test_actor_email_mismatch")

        print(f"MPS2_PRICING_TEST_ACTOR=PASS:email={TEST_USER_EMAIL}")
        print(
            "MPS2_PRICING_ACTOR_READY=PASS:"
            f"available_credits={available}:"
            f"tier={row['tier_code']}:plan={row['plan_code']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
