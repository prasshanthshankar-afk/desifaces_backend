from __future__ import annotations

import os
import asyncio
import logging
from urllib.parse import urlparse

import asyncpg

logger = logging.getLogger("svc-fusion.db")

_POOL: asyncpg.Pool | None = None
_LOCK = asyncio.Lock()


def _dsn_safe(dsn: str) -> str:
    try:
        u = urlparse(dsn)
        host = u.hostname or ""
        port = u.port or ""
        db = (u.path or "").lstrip("/")
        return f"{u.scheme}://***@{host}:{port}/{db}"
    except Exception:
        return "<invalid-dsn>"


async def init_pool() -> asyncpg.Pool:
    global _POOL
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")

    if _POOL is not None:
        return _POOL

    async with _LOCK:
        if _POOL is not None:
            return _POOL

        logger.info("Initializing asyncpg pool: %s", _dsn_safe(dsn))
        _POOL = await asyncpg.create_pool(
            dsn=dsn,
            min_size=int(os.getenv("DB_POOL_MIN", "1")),
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
            command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT", "30")),
        )
        return _POOL


async def get_pool() -> asyncpg.Pool:
    return await init_pool()