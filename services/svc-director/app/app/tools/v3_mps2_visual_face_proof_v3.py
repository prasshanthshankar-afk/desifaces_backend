from __future__ import annotations

import asyncio
import os
from uuid import UUID

from app.participant_face import compile_participant_face_studio_input
from app.tools import v3_mps2_visual_face_proof_v2 as proof


REQUIRED_CREDITS = 10
TEST_USER_EMAIL = str(
    os.getenv("DF_V3_E2E_TEST_USER_EMAIL") or "user_apple_iap_test1@desifaces.ai"
).strip().lower()


async def _active_actor(pool) -> tuple[UUID, UUID]:
    if not TEST_USER_EMAIL:
        raise RuntimeError("MPS2_VISUAL_PRECHECK_FAIL=test_user_email_missing")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            with spendable as (
              select user_id,
                coalesce(sum(greatest(remaining_amount-reserved_amount,0)) filter (
                  where status='active' and (expires_at is null or expires_at>now())
                ),0)::numeric as available_credits
              from public.pricing_credit_lots
              group by user_id
            )
            select bam.user_id,bam.billing_account_id,
                   coalesce(s.available_credits,0)::numeric as available_credits,
                   lower(u.email) as email
            from public.pricing_billing_account_members bam
            join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
            join core.users u on u.id=bam.user_id
            left join spendable s on s.user_id=bam.user_id
            where bam.status='active' and ba.status='active'
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
            f"MPS2_VISUAL_PRECHECK_FAIL=canonical_test_actor_not_found:{TEST_USER_EMAIL}"
        )
    available = int(row["available_credits"] or 0)
    if available < REQUIRED_CREDITS:
        raise RuntimeError(
            "MPS2_VISUAL_PRECHECK_FAIL=canonical_test_actor_underfunded:"
            f"required={REQUIRED_CREDITS}:available={available}"
        )
    if str(row["email"] or "").strip().lower() != TEST_USER_EMAIL:
        raise RuntimeError("MPS2_VISUAL_PRECHECK_FAIL=canonical_test_actor_email_mismatch")

    print(f"MPS2_VISUAL_TEST_ACTOR=PASS:email={TEST_USER_EMAIL}")
    print(f"MPS2_VISUAL_ACTOR_BALANCE=PASS:available_credits={available}")
    return UUID(str(row["user_id"])), UUID(str(row["billing_account_id"]))


proof._active_actor = _active_actor
proof.compile_participant_face_studio_input = compile_participant_face_studio_input


if __name__ == "__main__":
    try:
        asyncio.run(proof.main())
    except proof.ParticipantFaceBridgeError as exc:
        raise SystemExit(f"MPS2_VISUAL_FACE_BRIDGE_FAIL={exc}") from exc
