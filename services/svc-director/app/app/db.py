from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import settings

_business_pool: Optional[asyncpg.Pool] = None
_checkpoint_pool: Optional[AsyncConnectionPool] = None


def _encode_json_compat(value: Any) -> str:
    """Encode Python JSON while tolerating pre-serialized object/list values."""
    if isinstance(value, str):
        raw = value.strip()
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, (dict, list)):
                    return raw
            except Exception:
                pass
    return json.dumps(value, ensure_ascii=False, default=str)


async def _init_business_codecs(conn: asyncpg.Connection) -> None:
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name,
            encoder=_encode_json_compat,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def open_business_pool() -> asyncpg.Pool:
    global _business_pool
    if _business_pool is None:
        _business_pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=60,
            init=_init_business_codecs,
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
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
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
