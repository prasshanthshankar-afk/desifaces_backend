# services/svc-pricing/app/app/workers/reservation_expirer.py
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from app.config import settings
from app.db import ensure_db_pool, close_db_pool

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


async def _expire_batch(conn: asyncpg.Connection) -> int:
    """
    Atomically expire reservations whose expires_at < now() and status='reserved'.
    Uses FOR UPDATE SKIP LOCKED so multiple workers can run safely.
    """
    rows = await conn.fetch(
        """
        select id, user_id, reserved_credits, currency
        from pricing_credit_reservations
        where status = 'reserved'
          and expires_at < now()
        order by expires_at asc
        limit $1
        for update skip locked
        """,
        settings.RESERVATION_EXPIRE_BATCH,
    )
    if not rows:
        return 0

    expired = 0
    for r in rows:
        rid = UUID(str(r["id"]))
        user_id = UUID(str(r["user_id"]))
        held = int(r["reserved_credits"])
        currency = str(r.get("currency") or "")

        async with conn.transaction():
            # re-lock the reservation row
            rr = await conn.fetchrow(
                """
                select status, reserved_credits
                from pricing_credit_reservations
                where id=$1
                for update
                """,
                rid,
            )
            if not rr or str(rr["status"]) != "reserved":
                continue

            # Lock account row
            acc = await conn.fetchrow(
                "select reserved_credits from pricing_credit_accounts where user_id=$1 for update",
                user_id,
            )
            if acc:
                current_reserved = int(acc["reserved_credits"])
                new_reserved = max(0, current_reserved - held)
                await conn.execute(
                    "update pricing_credit_accounts set reserved_credits=$2, updated_at=now() where user_id=$1",
                    user_id, new_reserved,
                )

            await conn.execute(
                "update pricing_credit_reservations set status='expired', updated_at=now() where id=$1",
                rid,
            )

            # Ledger audit (idempotent by user+idempotency_key)
            await conn.execute(
                """
                insert into pricing_credit_ledger_events
                  (id, user_id, event_type, credits_delta, idempotency_key, currency, channel, metadata_json, created_at)
                values
                  (gen_random_uuid(), $1, 'reserve_release', 0, $2, $3, 'worker', $4::jsonb, now())
                on conflict (user_id, idempotency_key) do nothing
                """,
                user_id,
                f"reserve_expire:{rid}",
                currency or None,
                {"reservation_id": str(rid), "reason": "expired", "reserved_delta": -held},
            )

        expired += 1

    return expired


async def run_loop() -> None:
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    pool = await ensure_db_pool()
    logger.info("reservation_expirer started")

    try:
        while True:
            # small jitter to avoid stampeding if multiple workers are started together
            await asyncio.sleep(settings.EXPIRER_POLL_INTERVAL_S + random.uniform(0, settings.EXPIRER_JITTER_S))

            async with pool.acquire() as conn:
                try:
                    n = await _expire_batch(conn)
                    if n:
                        logger.info("expired reservations: %s", n)
                except asyncpg.UndefinedTableError:
                    logger.warning("pricing_credit_reservations table missing (migrations not applied yet)")
                except Exception as e:
                    logger.exception("expirer loop error: %s", e)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(run_loop())