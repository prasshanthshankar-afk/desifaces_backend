from __future__ import annotations

import json
from typing import Optional

import asyncpg

from app.config import settings

_pool: Optional[asyncpg.Pool] = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    # Ensure json/jsonb come back as Python dict/list instead of str
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool:
        return _pool

    dsn = settings.POSTGRES_DSN
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=getattr(settings, "DB_POOL_MIN", 1),
        max_size=getattr(settings, "DB_POOL_MAX", 10),
        init=_init_conn,
    )
    return _pool