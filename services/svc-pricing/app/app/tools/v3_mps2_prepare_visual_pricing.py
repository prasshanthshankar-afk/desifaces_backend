from __future__ import annotations

import asyncio
from decimal import Decimal

from app.db import ensure_db_pool
from app.services.subscription_credit_integrity_service import repair_active_subscription_credit_cycles


REQUIRED_CREDITS = Decimal("10")


async def main() -> None:
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
              lower(coalesce(be.plan_code,'free')) as plan_code
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
              and coalesce(s.available_credits,0) >= $1::numeric
            order by coalesce(s.available_credits,0) desc,
                     bam.is_default desc,
                     case bam.role when 'owner' then 0 when 'finance_admin' then 1 else 2 end,
                     bam.created_at asc
            limit 1
            """,
            REQUIRED_CREDITS,
        )
        if not row:
            best = await conn.fetchrow(
                """
                select coalesce(max(available),0)::numeric as best_available
                from (
                  select coalesce(sum(greatest(l.remaining_amount-l.reserved_amount,0)) filter (
                    where l.status='active' and (l.expires_at is null or l.expires_at>now())
                  ),0)::numeric as available
                  from public.pricing_billing_account_members bam
                  join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
                  left join public.pricing_credit_lots l on l.user_id=bam.user_id
                  where bam.status='active' and ba.status='active'
                  group by bam.user_id
                ) q
                """
            )
            raise RuntimeError(
                "MPS2_PRICING_PREP_FAIL=no_v3_actor_with_required_credits:"
                f"required={REQUIRED_CREDITS}:best_available={best['best_available'] if best else 0}"
            )

        print(
            "MPS2_PRICING_ACTOR_READY=PASS:"
            f"available_credits={row['available_credits']}:"
            f"tier={row['tier_code']}:plan={row['plan_code']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
