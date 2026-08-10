from __future__ import annotations

import asyncio
import logging
import signal
from typing import List

import asyncpg

from app.db import close_db_pool, get_pool, init_db_pool
from app.settings import settings

logger = logging.getLogger("dashboard_refresh_worker")

_stop = False


def _handle_stop(*_args):
    global _stop
    _stop = True


async def process_batch(conn: asyncpg.Connection, batch_size: int) -> int:
    # One transaction: claim rows, refresh cache, delete claimed
    async with conn.transaction():
        rows = await conn.fetch(
            """
            select user_id
            from public.dashboard_refresh_requests
            order by requested_at asc
            limit $1
            for update skip locked
            """,
            batch_size,
        )

        if not rows:
            return 0

        user_ids: List[str] = [str(r["user_id"]) for r in rows]

        for uid in user_ids:
            await conn.execute("select public.fn_dashboard_refresh_home_cache($1::uuid)", uid)

        await conn.execute(
            "delete from public.dashboard_refresh_requests where user_id = any($1::uuid[])",
            user_ids,
        )

        return len(user_ids)


async def run():
    await init_db_pool(settings.DATABASE_URL)
    pool = get_pool()

    try:
        while not _stop and settings.DASHBOARD_WORKER_ENABLED:
            try:
                async with pool.acquire() as conn:
                    processed = await process_batch(conn, settings.DASHBOARD_WORKER_BATCH_SIZE)
                if processed == 0:
                    await asyncio.sleep(settings.DASHBOARD_WORKER_POLL_SECONDS)
            except Exception as exc:
                logger.exception("dashboard refresh worker batch failed: %s", exc)
                await asyncio.sleep(1.5)
    finally:
        await close_db_pool()


def main():
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    asyncio.run(run())


if __name__ == "__main__":
    main()
