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
from app.services.reservations.reservation_service import release

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


async def _expire_batch(conn: asyncpg.Connection) -> int:
    """
    Release expired reservations through the canonical reservation lifecycle.

    This is intentionally the same release path used by the Pricing API so lot
    allocations, the legacy account summary and ledger audit stay consistent.
    Multiple workers are safe because release() is idempotent for terminal rows.
    """
    rows = await conn.fetch(
        """
        select id, user_id
        from pricing_credit_reservations
        where status = 'reserved'
          and expires_at < now()
        order by expires_at asc
        limit $1
        """,
        settings.RESERVATION_EXPIRE_BATCH,
    )
    if not rows:
        return 0

    expired = 0
    for row in rows:
        try:
            view = await release(
                conn,
                user_id=UUID(str(row["user_id"])),
                reservation_id=UUID(str(row["id"])),
                idempotency_key=None,
                channel="worker",
                country_code="",
                reason="expired",
            )
            if str(view.status) in {"released", "expired"}:
                expired += 1
        except ValueError as exc:
            if str(exc) in {
                "PRICING_RESERVATION_ALREADY_COMMITTED",
                "PRICING_RESERVATION_NOT_FOUND",
            }:
                continue
            raise

    return expired


async def run_loop() -> None:
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    pool = await ensure_db_pool()
    logger.info("reservation_expirer started")

    try:
        while True:
            # Process once immediately on startup so stale holds do not survive
            # until the first poll interval, then continue on the configured cadence.
            async with pool.acquire() as conn:
                try:
                    n = await _expire_batch(conn)
                    if n:
                        logger.info("expired reservations: %s", n)
                except asyncpg.UndefinedTableError:
                    logger.warning("pricing_credit_reservations table missing (migrations not applied yet)")
                except Exception as e:
                    logger.exception("expirer loop error: %s", e)

            # small jitter to avoid stampeding if multiple workers are started together
            await asyncio.sleep(settings.EXPIRER_POLL_INTERVAL_S + random.uniform(0, settings.EXPIRER_JITTER_S))
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(run_loop())