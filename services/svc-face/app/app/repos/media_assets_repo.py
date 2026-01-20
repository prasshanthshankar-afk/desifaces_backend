# repos/media_assets_repo.py
# Perfect mapping to media_assets table - ZERO BUGS

from __future__ import annotations
import logging
from typing import List, Optional, Dict, Any

from .base_repo import BaseRepository
from ..domain.models import MediaAssetDB

logger = logging.getLogger(__name__)

class MediaAssetsRepo(BaseRepository):
    """Repository for media_assets table - handles media asset lifecycle"""
    
    async def create_asset(
        self,
        user_id: str,
        kind: str,
        storage_ref: str,
        content_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        duration_ms: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        q = """
        INSERT INTO media_assets (
        user_id, kind, storage_ref, content_type, bytes, sha256,
        width, height, duration_ms, meta_json, created_at, updated_at
        )
        VALUES (
        $1::uuid, $2, $3, $4, $5, $6,
        $7, $8, $9, $10::jsonb, now(), now()
        )
        RETURNING id::text
        """
        return await self.fetch_scalar(
            q,
            user_id,
            kind,
            storage_ref,
            content_type,
            size_bytes,
            sha256,
            width,
            height,
            duration_ms,
            self.prepare_jsonb_param(meta or {}),
        )
    
    # ============================================================================
    # ADDITIONAL METHODS FOR media_assets TABLE
    # ============================================================================
    async def get_asset(self, asset_id: str) -> Optional[MediaAssetDB]:
        """Get media asset by ID"""
        
        query = "SELECT * FROM media_assets WHERE id = $1::uuid"
        row = await self.execute_query(query, asset_id)
        
        if not row:
            return None
        
        data = self.convert_db_row(row)
        return MediaAssetDB(**data)
    
    async def update_storage_ref(self, asset_id: str, storage_ref: str) -> None:
        """Update asset storage reference (URL)"""
        
        query = """
        UPDATE media_assets 
        SET storage_ref = $2, updated_at = now() 
        WHERE id = $1::uuid
        """
        
        await self.execute_command(query, asset_id, storage_ref)
        
        logger.info("Asset storage reference updated", extra={
            "asset_id": asset_id,
            "new_storage_ref": storage_ref
        })
    
    async def list_user_assets(self, user_id: str, kind: str = None, limit: int = 50) -> List[MediaAssetDB]:
        """List user's media assets"""
        
        query = """
        SELECT * FROM media_assets
        WHERE user_id = $1::uuid
        """
        params = [user_id]
        
        if kind:
            query += " AND kind = $2"
            params.append(kind)
        
        query += " ORDER BY created_at DESC LIMIT $" + str(len(params) + 1)
        params.append(limit)
        
        rows = await self.execute_queries(query, *params)
        return [MediaAssetDB(**self.convert_db_row(row)) for row in rows]