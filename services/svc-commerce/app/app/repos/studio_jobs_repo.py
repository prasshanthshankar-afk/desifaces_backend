from __future__ import annotations

import hashlib
from typing import Any, Dict
from uuid import UUID

from app.db import get_pool

_SCHEMA = "public"
_TABLE = f"{_SCHEMA}.studio_jobs"


def make_request_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


class StudioJobsRepo:
    async def create_or_get(
        self,
        *,
        user_id: UUID,
        studio_type: str,
        request_hash: str,
        payload_json: Dict[str, Any],
        meta_json: Dict[str, Any],
        status: str = "queued",
    ) -> UUID:
        pool = await get_pool()
        row = await pool.fetchrow(
            f"""
            INSERT INTO {_TABLE}(studio_type, status, request_hash, payload_json, meta_json, user_id)
            VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6)
            ON CONFLICT (user_id, studio_type, request_hash) DO NOTHING
            RETURNING id
            """,
            studio_type,
            status,
            request_hash,
            payload_json,
            meta_json,
            user_id,
        )
        if row:
            return row["id"]

        row2 = await pool.fetchrow(
            f"""
            SELECT id
            FROM {_TABLE}
            WHERE user_id=$1 AND studio_type=$2 AND request_hash=$3
            """,
            user_id,
            studio_type,
            request_hash,
        )
        return row2["id"]