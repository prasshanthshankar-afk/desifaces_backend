# services/svc-face/app/app/repos/face_profiles_repo.py
from __future__ import annotations
import json
from typing import Any, Dict, Optional, List
import asyncpg

class FaceProfilesRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create_profile(
        self,
        user_id: str,
        display_name: Optional[str],
        primary_image_asset_id: str,
        attributes_json: Dict[str, Any],
        meta_json: Dict[str, Any]
    ) -> str:
        """Create a face profile"""
        sql = """
        INSERT INTO face_profiles 
        (user_id, display_name, primary_image_asset_id, attributes_json, meta_json)
        VALUES ($1::uuid, $2, $3::uuid, $4::jsonb, $5::jsonb)
        RETURNING id::text
        """
        attributes_str = json.dumps(attributes_json)
        meta_str = json.dumps(meta_json)
        
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                sql, user_id, display_name, primary_image_asset_id, 
                attributes_str, meta_str
            )

    async def link_job_output(self, job_id: str, face_profile_id: str, output_asset_id: Optional[str] = None) -> None:
        """Link face profile to job"""
        sql = """
        INSERT INTO face_job_outputs (job_id, face_profile_id, output_asset_id)
        VALUES ($1::uuid, $2::uuid, $3::uuid)
        ON CONFLICT (job_id) DO NOTHING
        """
        async with self.pool.acquire() as conn:
            await conn.execute(sql, job_id, face_profile_id, output_asset_id)

    async def get_profile(self, face_profile_id: str) -> Optional[asyncpg.Record]:
        """Get face profile by ID"""
        sql = """
        SELECT fp.*, ma.storage_ref as image_url
        FROM face_profiles fp
        LEFT JOIN media_assets ma ON fp.primary_image_asset_id = ma.id
        WHERE fp.id = $1::uuid
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(sql, face_profile_id)

    async def list_user_profiles(self, user_id: str, limit: int = 50) -> List[asyncpg.Record]:
        """List user's face profiles"""
        sql = """
        SELECT fp.id, fp.display_name, fp.attributes_json, fp.meta_json, ma.storage_ref as image_url
        FROM face_profiles fp
        LEFT JOIN media_assets ma ON fp.primary_image_asset_id = ma.id
        WHERE fp.user_id = $1::uuid AND fp.status = 'active'
        ORDER BY fp.created_at DESC
        LIMIT $2
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, user_id, limit)

    async def get_job_faces(self, job_id: str) -> List[asyncpg.Record]:
        """Get all face profiles for a job - FIXED with storage_ref"""
        sql = """
        SELECT 
            fp.id, 
            fp.display_name, 
            fp.attributes_json, 
            fp.meta_json,
            ma.storage_ref as image_url
        FROM face_job_outputs fjo
        JOIN face_profiles fp ON fjo.face_profile_id = fp.id
        LEFT JOIN media_assets ma ON fp.primary_image_asset_id = ma.id
        WHERE fjo.job_id = $1::uuid
        ORDER BY fp.created_at
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, job_id)
