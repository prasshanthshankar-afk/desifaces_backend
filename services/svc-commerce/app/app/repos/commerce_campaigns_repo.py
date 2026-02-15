from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from app.db import get_pool

_SCHEMA = "public"
_TABLE = f"{_SCHEMA}.commerce_campaigns"


class CommerceCampaignsRepo:
    async def find_by_idempotency(
        self, *, user_id: UUID, quote_id: UUID, idempotency_key: str
    ) -> Optional[Dict[str, Any]]:
        pool = await get_pool()
        row = await pool.fetchrow(
            f"""
            SELECT *
            FROM {_TABLE}
            WHERE user_id=$1 AND quote_id=$2
              AND (meta_json->>'idempotency_key') = $3
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id,
            quote_id,
            idempotency_key,
        )
        return dict(row) if row else None

    async def create(
        self,
        *,
        user_id: UUID,
        mode: str,
        product_type: str,
        quote_id: UUID,
        input_json: Dict[str, Any],
        meta_json: Dict[str, Any],
        status: str = "queued",
    ) -> UUID:
        pool = await get_pool()
        row = await pool.fetchrow(
            f"""
            INSERT INTO {_TABLE}(user_id, mode, product_type, status, quote_id, input_json, meta_json)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb)
            RETURNING id
            """,
            user_id,
            mode,
            product_type,
            status,
            quote_id,
            input_json,
            meta_json,
        )
        return row["id"