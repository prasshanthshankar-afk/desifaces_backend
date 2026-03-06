# services/svc-marketing/app/app/repos/marketing_platform_posts_repo.py
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg


class MarketingPlatformPostsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def upsert_post(
        self,
        run_id: UUID,
        platform: str,
        media_id: Optional[str],
        permalink: Optional[str],
        status: str,
        payload_json: Dict[str, Any],
    ) -> UUID:
        q = """
        insert into marketing_platform_posts (run_id, platform, media_id, permalink, status, published_at, payload_json)
        values ($1, $2, $3, $4, $5, now(), $6::jsonb)
        returning platform_post_id
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, run_id, platform, media_id, permalink, status, json.dumps(payload_json))
            return row["platform_post_id"]

    async def find_by_media_id(self, platform: str, media_id: str) -> Optional[asyncpg.Record]:
        q = "select * from marketing_platform_posts where platform=$1 and media_id=$2 order by created_at desc limit 1"
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(q, platform, media_id)