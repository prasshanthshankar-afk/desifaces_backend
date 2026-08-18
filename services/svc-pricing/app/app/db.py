# services/svc-pricing/app/app/db.py
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

_DB_POOL: Optional[asyncpg.Pool] = None


def _encode_json_compat(value: Any) -> str:
    """Encode JSON values while tolerating legacy pre-serialized objects/lists.

    svc-pricing historically contains a mix of callers that pass Python
    dict/list values and callers that already call ``json.dumps`` before
    binding to a JSON/JSONB parameter. Because asyncpg's codec encoder also
    serializes the value, blindly dumping an already-serialized object stores a
    JSON string scalar instead of the intended JSON object. That breaks JSONB
    operators such as ``metadata_json->>'cycle_key'`` and was the root cause of
    subscription-cycle credits being carried into the next renewal period.

    Only strings that decode to an object/list are treated as already-encoded
    JSON. Ordinary strings remain ordinary JSON string values.
    """
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


async def _init_codecs(conn: asyncpg.Connection) -> None:
    # Ensure json/jsonb come back as Python dict/list (like svc-marketing).
    # The compatibility encoder also prevents accidental double serialization
    # by older pricing call sites that already pass json.dumps(...) output.
    await conn.set_type_codec(
        "json",
        encoder=_encode_json_compat,
        decoder=lambda s: json.loads(s),
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=_encode_json_compat,
        decoder=lambda s: json.loads(s),
        schema="pg_catalog",
    )


async def ensure_db_pool() -> asyncpg.Pool:
    global _DB_POOL
    if _DB_POOL is not None:
        return _DB_POOL

    logger.info("Creating asyncpg pool...")
    _DB_POOL = await asyncpg.create_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN_SIZE,
        max_size=settings.DB_POOL_MAX_SIZE,
        command_timeout=settings.DB_COMMAND_TIMEOUT_S,
        init=_init_codecs,
    )
    return _DB_POOL


async def close_db_pool() -> None:
    global _DB_POOL
    if _DB_POOL is not None:
        await _DB_POOL.close()
        _DB_POOL = None


async def fetch_one(conn: asyncpg.Connection, q: str, *args: Any) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(q, *args)


async def fetch_all(conn: asyncpg.Connection, q: str, *args: Any) -> list[asyncpg.Record]:
    return await conn.fetch(q, *args)
