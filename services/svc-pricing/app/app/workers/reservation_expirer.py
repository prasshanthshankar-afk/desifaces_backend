# services/svc-pricing/app/app/workers/reservation_expirer.py
from __future__ import annotations

import asyncio
import logging
import random
from uuid import UUID

import asyncpg

from app.config import settings
from app.db import ensure_db_pool, close_db_pool
from app.services.reservations.reservation_service import release

logger = logging.getLogger(__name__)


async def _expire_batch(conn: asyncpg.Connection) -> int:
    """Release expired holds using canonical credit-lot-aware settlement.

    Selection is advisory only: release(reason='expired') must recheck expiry
    under the reservation row lock to reject stale selections after renewal.
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
                "PRICING_RESERVATION_RENEWED_NOT_EXPIRED",
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
            async with pool.acquire() as conn:
                try:
                    n = await _expire_batch(conn)
                    if n:
                        logger.info("expired reservations: %s", n)
                except asyncpg.UndefinedTableError:
                    logger.warning("pricing_credit_reservations table missing (migrations not applied yet)")
                except Exception:
                    logger.exception("expirer loop error")
            await asyncio.sleep(
                settings.EXPIRER_POLL_INTERVAL_S
                + random.uniform(0, settings.EXPIRER_JITTER_S)
            )
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(run_loop())
