from __future__ import annotations

from typing import Optional

import asyncpg
from psycopg_pool import AsyncConnectionPool

from .config import settings

_business_pool: Optional[asyncpg.Pool] = None
_checkpoint_pool: Optional[AsyncConnectionPool] = None


async def open_business_pool() -> asyncpg.Pool:
    global _business_pool
    if _business_pool is None:
        _business_pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=60,
        )
    return _business_pool


async def open_checkpoint_pool() -> AsyncConnectionPool:
    global _checkpoint_pool
    if _checkpoint_pool is None:
        _checkpoint_pool = AsyncConnectionPool(
            conninfo=settings.DATABASE_URL,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True},
        )
        await _checkpoint_pool.open(wait=True)
    return _checkpoint_pool


async def close_pools() -> None:
    global _business_pool, _checkpoint_pool
    if _business_pool is not None:
        await _business_pool.close()
        _business_pool = None
    if _checkpoint_pool is not None:
        await _checkpoint_pool.close()
        _checkpoint_pool = None
