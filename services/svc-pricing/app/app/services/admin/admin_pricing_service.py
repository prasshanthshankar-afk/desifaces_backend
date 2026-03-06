# services/svc-pricing/app/app/services/admin/admin_pricing_service.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class GrantReceipt:
    user_id: UUID
    credits_granted: int
    balance_after: int


async def _ensure_account_row(conn: asyncpg.Connection, user_id: UUID) -> None:
    await conn.execute(
        """
        insert into pricing_credit_accounts(user_id, balance_credits, reserved_credits, updated_at)
        values($1, 0, 0, now())
        on conflict (user_id) do nothing
        """,
        user_id,
    )


async def grant_credits(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    credits: int,
    idempotency_key: str,
    currency: Optional[str] = None,
    money_amount: Optional[Decimal] = None,
    metadata: Optional[dict] = None,
) -> GrantReceipt:
    if credits <= 0:
        raise ValueError("credits_must_be_positive")

    md = metadata or {}

    async with conn.transaction():
        await _ensure_account_row(conn, user_id)

        # Lock account row
        acc = await conn.fetchrow(
            "select balance_credits, reserved_credits from pricing_credit_accounts where user_id=$1 for update",
            user_id,
        )
        _ = int(acc["balance_credits"])  # bal_before (kept for future audit if needed)

        # Idempotent ledger insert
        await conn.execute(
            """
            insert into pricing_credit_ledger_events
              (id, user_id, event_type, credits_delta, idempotency_key, currency, money_amount, channel, metadata_json, created_at)
            values
              (gen_random_uuid(), $1, 'grant', $2, $3, $4, $5, 'admin', $6::jsonb, now())
            on conflict (user_id, idempotency_key) do nothing
            """,
            user_id, int(credits), idempotency_key, currency, money_amount, md,
        )

        # Apply-once guard (mark ledger row as applied)
        applied = await conn.fetchrow(
            """
            select 1
            from pricing_credit_ledger_events
            where user_id=$1 and idempotency_key=$2 and metadata_json->>'applied'='true'
            """,
            user_id, idempotency_key,
        )
        if not applied:
            await conn.execute(
                """
                update pricing_credit_accounts
                set balance_credits = balance_credits + $2,
                    updated_at = now()
                where user_id = $1
                """,
                user_id, int(credits),
            )
            await conn.execute(
                """
                update pricing_credit_ledger_events
                set metadata_json = coalesce(metadata_json,'{}'::jsonb) || '{"applied":"true"}'::jsonb
                where user_id=$1 and idempotency_key=$2
                """,
                user_id, idempotency_key,
            )

        acc2 = await conn.fetchrow(
            "select balance_credits from pricing_credit_accounts where user_id=$1",
            user_id,
        )
        return GrantReceipt(user_id=user_id, credits_granted=credits, balance_after=int(acc2["balance_credits"]))


async def set_user_tier(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    tier_code: str,
    metadata: Optional[dict] = None,
    sync_core_users_tier: bool = True,
) -> None:
    """
    Pricing tier may include future tiers (developer, api_enterprise, etc.).
    core.users.tier currently only allows: free|pro|enterprise.
    So:
      - always write pricing_user_entitlements
      - optionally sync core.users.tier only if tier_code is allowed there
    """
    md = metadata or {}

    await conn.execute(
        """
        insert into pricing_user_entitlements(user_id, tier_code, effective_from, metadata_json)
        values($1, $2, now(), $3::jsonb)
        on conflict (user_id)
        do update set tier_code=excluded.tier_code, effective_from=excluded.effective_from, metadata_json=excluded.metadata_json
        """,
        user_id, tier_code, md,
    )

    if sync_core_users_tier and tier_code in {"free", "pro", "enterprise"}:
        await conn.execute(
            "update core.users set tier=$2, updated_at=now() where id=$1",
            user_id, tier_code,
        )


async def upsert_feature_flag(
    conn: asyncpg.Connection,
    *,
    code: str,
    enabled: bool,
    billing_mode: str,
    scope: str = "global",
    country_code: str = "",
    tier_code: str = "",
    channel: str = "",
    priority: int = 1000,
    metadata: Optional[dict] = None,
) -> None:
    if billing_mode not in {"disabled", "shadow", "free", "bill"}:
        raise ValueError("invalid_billing_mode")

    await conn.execute(
        """
        insert into pricing_feature_flags
          (code, scope, country_code, tier_code, channel, enabled, billing_mode, priority, effective_from, metadata_json)
        values
          ($1, $2, $3, $4, $5, $6, $7, $8, now(), $9::jsonb)
        on conflict (code)
        do update set
          scope=excluded.scope,
          country_code=excluded.country_code,
          tier_code=excluded.tier_code,
          channel=excluded.channel,
          enabled=excluded.enabled,
          billing_mode=excluded.billing_mode,
          priority=excluded.priority,
          effective_from=excluded.effective_from,
          metadata_json=excluded.metadata_json
        """,
        code, scope, country_code, tier_code, channel, enabled, billing_mode, priority, (metadata or {}),
    )