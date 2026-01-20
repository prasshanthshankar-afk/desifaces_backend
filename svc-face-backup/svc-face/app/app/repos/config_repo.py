    # services/svc-face/app/app/repos/config_repo.py
from __future__ import annotations
from typing import List, Optional, Dict, Any
import asyncpg
import json

class FaceConfigRepo:
    """Repository for face generation configuration data"""
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
    
    async def get_regions(self, language: str = "en", active_only: bool = True) -> List[Dict[str, Any]]:
        """Get all available regions"""
        query = """
        SELECT 
            code,
            display_name->>$1 as display_name,
            sub_region,
            ethnicity_notes,
            typical_skin_tones,
            prompt_base
        FROM face_generation_regions
        WHERE is_active = $2 OR $2 = false
        ORDER BY sort_order, code
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, language, active_only)
            return [dict(r) for r in rows]
    
    async def get_region_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        """Get specific region config"""
        query = """
        SELECT * FROM face_generation_regions WHERE code = $1 AND is_active = true
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, code)
            return dict(row) if row else None
    
    async def get_skin_tones(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """Get skin tone configurations prioritized by diversity weight"""
        query = """
        SELECT code, prompt_descriptor, diversity_weight
        FROM face_generation_skin_tones
        WHERE is_active = $1 OR $1 = false
        ORDER BY diversity_weight DESC, RANDOM()
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, active_only)
            return [dict(r) for r in rows]
    
    async def get_facial_features(self, feature_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get facial features for diversity"""
        if feature_type:
            query = """
            SELECT feature_type, code, prompt_descriptor
            FROM face_generation_features
            WHERE feature_type = $1 AND is_active = true
            ORDER BY RANDOM()
            """
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, feature_type)
        else:
            query = """
            SELECT feature_type, code, prompt_descriptor
            FROM face_generation_features
            WHERE is_active = true
            ORDER BY RANDOM()
            """
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)
        return [dict(r) for r in rows]
    
    async def get_contexts(self, glamour_level: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get socioeconomic contexts"""
        if glamour_level:
            query = """
            SELECT code, economic_class, setting_type, prompt_modifiers, glamour_level
            FROM face_generation_contexts
            WHERE glamour_level = $1 AND is_active = true
            """
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, glamour_level)
        else:
            query = """
            SELECT code, economic_class, setting_type, prompt_modifiers, glamour_level
            FROM face_generation_contexts
            WHERE is_active = true
            ORDER BY RANDOM()
            """
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)
        return [dict(r) for r in rows]
    
    async def get_context_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        """Get specific context"""
        query = """
        SELECT * FROM face_generation_contexts WHERE code = $1 AND is_active = true
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, code)
            return dict(row) if row else None
    
    async def get_clothing_styles(self, 
                                  category: Optional[str] = None,
                                  gender_fit: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get clothing styles"""
        conditions = ["is_active = true"]
        params = []
        param_count = 1
        
        if category:
            conditions.append(f"category = ${param_count}")
            params.append(category)
            param_count += 1
        
        if gender_fit:
            conditions.append(f"(gender_fit = ${param_count} OR gender_fit = 'neutral')")
            params.append(gender_fit)
        
        query = f"""
        SELECT code, category, prompt_descriptor, formality_level
        FROM face_generation_clothing
        WHERE {' AND '.join(conditions)}
        ORDER BY RANDOM()
        """
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]