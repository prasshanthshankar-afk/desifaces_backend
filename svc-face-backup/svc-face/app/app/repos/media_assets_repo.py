# services/svc-face/app/app/repos/media_assets_repo.py
from __future__ import annotations
import json
from typing import Any, Dict, Optional
import asyncpg

class MediaAssetsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create_asset(
        self,
        user_id: str,
        kind: str,
        url: str,
        storage_path: str,
        content_type: str,
        size_bytes: int,
        meta_json: Dict[str, Any]
    ) -> str:
        """Create a media asset record - url parameter is stored as storage_ref"""
        sql = """
        INSERT INTO media_assets 
        (user_id, kind, storage_ref, content_type, bytes, meta_json)
        VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb)
        RETURNING id::text
        """
        meta_str = json.dumps(meta_json)
        
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                sql, user_id, kind, url, content_type, size_bytes, meta_str
            )

    async def get_asset(self, asset_id: str) -> Optional[asyncpg.Record]:
        """Get media asset by ID"""
        sql = "SELECT * FROM media_assets WHERE id = $1::uuid"
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(sql, asset_id)

    async def update_url(self, asset_id: str, url: str) -> None:
        """Update asset URL"""
        sql = "UPDATE media_assets SET storage_ref = $2, updated_at = now() WHERE id = $1::uuid"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, asset_id, url)
