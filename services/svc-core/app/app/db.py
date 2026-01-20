from __future__ import annotations

import os
import asyncpg
from typing import Optional

_POOL: Optional[asyncpg.Pool] = None

async def init_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL:
        return _POOL
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")
    _POOL = await asyncpg.create_pool(
        dsn=dsn,
        min_size=int(os.getenv("DB_POOL_MIN", "1")),
        max_size=int(os.getenv("DB_POOL_MAX", "10")),
        command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT", "30")),
    )
    return _POOL

async def get_pool() -> asyncpg.Pool:
    if not _POOL:
        return await init_pool()
    return _POOL