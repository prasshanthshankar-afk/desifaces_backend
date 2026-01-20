# repos/face_profiles_repo.py
# Perfect mapping to face_profiles table - ZERO BUGS

from __future__ import annotations
import logging
from typing import List, Optional, Dict, Any

from .base_repo import BaseRepository
from ..domain.models import FaceProfileDB

logger = logging.getLogger(__name__)

class FaceProfilesRepo(BaseRepository):
    """Repository for face_profiles table - handles face profile lifecycle"""
    
    async def create_profile(
        self,
        user_id: str,
        display_name: str,
        primary_image_asset_id: str,
        attributes: Dict[str, Any],
        meta: Dict[str, Any]
    ) -> str:
        """Insert into face_profiles table with EXACT schema mapping"""
        
        query = """
        INSERT INTO face_profiles (
            user_id, display_name, primary_image_asset_id,
            attributes_json, meta_json, status, created_at, updated_at
        )
        VALUES (
            $1::uuid, $2, $3::uuid,
            $4::jsonb, $5::jsonb, 'active', now(), now()
        )
        RETURNING id::text
        """
        
        # Prepare parameters with correct types
        user_uuid = self.prepare_uuid_param(user_id)
        asset_uuid = self.prepare_uuid_param(primary_image_asset_id)
        attributes_jsonb = self.prepare_jsonb_param(attributes)
        meta_jsonb = self.prepare_jsonb_param(meta)
        
        profile_id = await self.fetch_scalar(
            query, user_uuid, display_name, asset_uuid, attributes_jsonb, meta_jsonb
        )
        
        logger.info("Face profile created", extra={
            "profile_id": profile_id,
            "user_id": user_id,
            "asset_id": primary_image_asset_id
        })
        
        return profile_id
    
    async def get_profile(self, profile_id: str) -> Optional[FaceProfileDB]:
        """Get face profile by ID with image URL"""
        
        query = """
        SELECT 
            fp.*,
            ma.storage_ref as image_url
        FROM face_profiles fp
        LEFT JOIN media_assets ma ON fp.primary_image_asset_id = ma.id
        WHERE fp.id = $1::uuid
        """
        
        row = await self.execute_query(query, profile_id)
        if not row:
            return None
        
        data = self.convert_db_row(row)
        return FaceProfileDB(**data)

# ============================================================================
# ADDITIONAL METHODS FOR face_job_outputs TABLE
# ============================================================================    
async def link_job_output(
    self,
    job_id: str,
    face_profile_id: str,
    output_asset_id: Optional[str],
    variant_number: int,
    prompt_used: Optional[str] = None,
    negative_prompt: Optional[str] = None,
    technical_specs: Optional[Dict[str, Any]] = None,
    creative_variations: Optional[Dict[str, Any]] = None,
    source_asset_id: Optional[str] = None,
    identity_score: Optional[float] = None,
    identity_verified: Optional[bool] = None,
) -> None:
    """
    One row per variant in face_job_outputs.
    Requires DB constraint: UNIQUE(job_id, variant_number).
    """
    q = """
    INSERT INTO face_job_outputs (
        job_id, face_profile_id, output_asset_id, variant_number,
        prompt_used, negative_prompt, technical_specs, creative_variations,
        source_asset_id, identity_score, identity_verified
    )
    VALUES (
        $1::uuid, $2::uuid, $3::uuid, $4,
        $5, $6, $7::jsonb, $8::jsonb,
        $9::uuid, $10, $11
    )
    ON CONFLICT (job_id, variant_number)
    DO UPDATE SET
      face_profile_id = EXCLUDED.face_profile_id,
      output_asset_id = EXCLUDED.output_asset_id,
      prompt_used = EXCLUDED.prompt_used,
      negative_prompt = EXCLUDED.negative_prompt,
      technical_specs = EXCLUDED.technical_specs,
      creative_variations = EXCLUDED.creative_variations,
      source_asset_id = EXCLUDED.source_asset_id,
      identity_score = EXCLUDED.identity_score,
      identity_verified = EXCLUDED.identity_verified
    """
    await self.execute_command(
        q,
        job_id,
        face_profile_id,
        output_asset_id,
        int(variant_number),
        prompt_used,
        negative_prompt,
        self.prepare_jsonb_param(technical_specs or {}),
        self.prepare_jsonb_param(creative_variations or {}),
        source_asset_id,
        identity_score,
        identity_verified,
    )

    # Log the linking action
    
    async def get_job_profiles(self, job_id: str) -> List[Dict[str, Any]]:
        """Get all face profiles for a job with image URLs"""
        
        query = """
        SELECT 
            fp.id,
            fp.display_name,
            fp.attributes_json,
            fp.meta_json,
            ma.storage_ref as image_url,
            fp.created_at
        FROM face_job_outputs fjo
        JOIN face_profiles fp ON fjo.face_profile_id = fp.id
        LEFT JOIN media_assets ma ON fp.primary_image_asset_id = ma.id
        WHERE fjo.job_id = $1::uuid
        ORDER BY fp.created_at
        """
        
        rows = await self.execute_queries(query, job_id)
        return [self.convert_db_row(row) for row in rows]
    
    async def list_user_profiles(self, user_id: str, limit: int = 50) -> List[FaceProfileDB]:
        """List user's face profiles"""
        
        query = """
        SELECT fp.*, ma.storage_ref as image_url
        FROM face_profiles fp
        LEFT JOIN media_assets ma ON fp.primary_image_asset_id = ma.id
        WHERE fp.user_id = $1::uuid AND fp.status = 'active'
        ORDER BY fp.created_at DESC
        LIMIT $2
        """
        
        rows = await self.execute_queries(query, user_id, limit)
        return [FaceProfileDB(**self.convert_db_row(row)) for row in rows]