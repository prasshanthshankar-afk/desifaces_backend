from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import asyncpg


class MarketingAssetsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def add_asset(
        self,
        run_id: UUID,
        kind: str,
        url: str,
        content_type: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
        duration_sec: Optional[float] = None,
        metadata_json: Optional[Dict[str, Any]] = None,
    ) -> UUID:
        asset_id = uuid4()
        q = """
        insert into marketing_assets (
          asset_id, run_id, kind, url, content_type, width, height, duration_sec, metadata_json
        ) values (
          $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb
        )
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                asset_id,
                run_id,
                kind,
                url,
                content_type,
                width,
                height,
                duration_sec,
                json.dumps(metadata_json or {}),
            )
        return asset_id

    async def list_assets(self, run_id: UUID):
        q = "select * from marketing_assets where run_id=$1 order by created_at asc"
        async with self.pool.acquire() as conn:
            return await conn.fetch(q, run_id)