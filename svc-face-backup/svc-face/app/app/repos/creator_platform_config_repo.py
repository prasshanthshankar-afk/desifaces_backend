
from __future__ import annotations
from typing import Dict, List, Any, Optional, Tuple
import asyncpg
import json
from app.domain.creator_platform_models import (
    ImageFormatConfig, UseCaseConfig, CreativeVariationConfig, 
    AgeRangeConfig, PlatformRequirementsConfig, CreatorPlatformConfig
)

class CreatorPlatformConfigRepo:
    """
    Repository for creator platform configuration.
    Provides database-driven configuration for all creator platform features.
    """
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
    
    def _convert_db_row(self, row):
        """Convert PostgreSQL types to Python types"""
        import json
        data = dict(row)
        data['id'] = str(data['id'])  # UUID to string
        
        # JSON strings to dicts
        for field in ['display_name', 'technical_specs', 'safe_zones']:
            if field in data and isinstance(data[field], str):
                data[field] = json.loads(data[field])
        return data

    # ============================================================================
    # IMAGE FORMATS REPOSITORY
    # ============================================================================
    
    async def get_image_formats(self, platform_category: Optional[str] = None) -> List[ImageFormatConfig]:
        """Get all active image formats, optionally filtered by platform category"""
        query = """
        SELECT id, code, display_name, width, height, aspect_ratio, 
               platform_category, recommended_platforms, technical_specs, 
               safe_zones, is_active, sort_order
        FROM face_generation_image_formats 
        WHERE is_active = true
        """
        params = []
        
        if platform_category:
            query += " AND platform_category = $1"
            params.append(platform_category)
            
        query += " ORDER BY sort_order, display_name->>'en'"
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [ImageFormatConfig(**self._convert_db_row(row)) for row in rows]
    
    async def get_image_format_by_code(self, code: str) -> Optional[ImageFormatConfig]:
        """Get specific image format by code"""
        query = """
        SELECT id, code, display_name, width, height, aspect_ratio,
               platform_category, recommended_platforms, technical_specs,
               safe_zones, is_active, sort_order
        FROM face_generation_image_formats 
        WHERE code = $1 AND is_active = true
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, code)
            return ImageFormatConfig(**self._convert_db_row(row)) if row else None
    
    async def get_formats_for_platform(self, platform_code: str) -> List[ImageFormatConfig]:
        """Get image formats compatible with specific platform"""
        query = """
        SELECT id, code, display_name, width, height, aspect_ratio,
               platform_category, recommended_platforms, technical_specs,
               safe_zones, is_active, sort_order
        FROM face_generation_image_formats 
        WHERE is_active = true 
        AND $1 = ANY(recommended_platforms)
        ORDER BY sort_order
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, platform_code)
            return [ImageFormatConfig(**dict(row)) for row in rows]
    
    # ============================================================================
    # USE CASES REPOSITORY
    # ============================================================================
    
    async def get_use_cases(self, category: Optional[str] = None) -> List[UseCaseConfig]:
        """Get all active use cases, optionally filtered by category"""
        query = """
        SELECT id, code, display_name, category, description, prompt_base,
               lighting_style, composition_style, mood_descriptors, background_type,
               recommended_formats, target_audience, industry_focus, is_active, sort_order
        FROM face_generation_use_cases
        WHERE is_active = true
        """
        params = []
        
        if category:
            query += " AND category = $1"
            params.append(category)
            
        query += " ORDER BY sort_order, display_name->>'en'"
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [UseCaseConfig(**dict(row)) for row in rows]
    
    async def get_use_case_by_code(self, code: str) -> Optional[UseCaseConfig]:
        """Get specific use case by code"""
        query = """
        SELECT id, code, display_name, category, description, prompt_base,
               lighting_style, composition_style, mood_descriptors, background_type,
               recommended_formats, target_audience, industry_focus, is_active, sort_order
        FROM face_generation_use_cases
        WHERE code = $1 AND is_active = true
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, code)
            return UseCaseConfig(**dict(row)) if row else None
    
    async def get_use_cases_for_format(self, format_code: str) -> List[UseCaseConfig]:
        """Get use cases compatible with specific image format"""
        query = """
        SELECT id, code, display_name, category, description, prompt_base,
               lighting_style, composition_style, mood_descriptors, background_type,
               recommended_formats, target_audience, industry_focus, is_active, sort_order
        FROM face_generation_use_cases
        WHERE is_active = true 
        AND $1 = ANY(recommended_formats)
        ORDER BY sort_order
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, format_code)
            return [UseCaseConfig(**dict(row)) for row in rows]
    
    # ============================================================================
    # CREATIVE VARIATIONS REPOSITORY
    # ============================================================================
    
    async def get_creative_variations(
        self, 
        variation_type: Optional[str] = None,
        use_case_code: Optional[str] = None
    ) -> List[CreativeVariationConfig]:
        """Get creative variations, optionally filtered by type or use case compatibility"""
        query = """
        SELECT id, variation_type, code, display_name, prompt_modifier,
               use_case_compatibility, mood_impact, professional_level,
               creativity_level, is_active
        FROM face_generation_variations
        WHERE is_active = true
        """
        params = []
        
        if variation_type:
            query += " AND variation_type = $1"
            params.append(variation_type)
            
        if use_case_code:
            if params:
                query += f" AND ${len(params)+1} = ANY(use_case_compatibility)"
            else:
                query += " AND $1 = ANY(use_case_compatibility)"
            params.append(use_case_code)
        
        query += " ORDER BY variation_type, display_name->>'en'"
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [CreativeVariationConfig(**dict(row)) for row in rows]
    
    async def get_variations_by_type(self, variation_type: str) -> List[CreativeVariationConfig]:
        """Get all variations of a specific type (lighting, pose, expression, styling)"""
        return await self.get_creative_variations(variation_type=variation_type)
    
    async def get_compatible_variations(
        self, 
        use_case_code: str, 
        professional_level_min: int = 1,
        creativity_level_min: int = 1
    ) -> Dict[str, List[CreativeVariationConfig]]:
        """Get variations compatible with use case, grouped by variation type"""
        query = """
        SELECT id, variation_type, code, display_name, prompt_modifier,
               use_case_compatibility, mood_impact, professional_level,
               creativity_level, is_active
        FROM face_generation_variations
        WHERE is_active = true 
        AND $1 = ANY(use_case_compatibility)
        AND professional_level >= $2
        AND creativity_level >= $3
        ORDER BY variation_type, professional_level DESC, creativity_level DESC
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, use_case_code, professional_level_min, creativity_level_min)
            
        # Group by variation type
        variations_by_type = {}
        for row in rows:
            variation = CreativeVariationConfig(**dict(row))
            variation_type = variation.variation_type
            if variation_type not in variations_by_type:
                variations_by_type[variation_type] = []
            variations_by_type[variation_type].append(variation)
            
        return variations_by_type
    
    # ============================================================================
    # AGE RANGES REPOSITORY
    # ============================================================================
    
    async def get_age_ranges(self) -> List[AgeRangeConfig]:
        """Get all active age ranges"""
        query = """
        SELECT id, code, display_name, min_age, max_age, prompt_descriptor,
               professional_contexts, is_active
        FROM face_generation_age_ranges
        WHERE is_active = true
        ORDER BY min_age
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [AgeRangeConfig(**dict(row)) for row in rows]
    
    async def get_age_range_by_code(self, code: str) -> Optional[AgeRangeConfig]:
        """Get specific age range by code"""
        query = """
        SELECT id, code, display_name, min_age, max_age, prompt_descriptor,
               professional_contexts, is_active
        FROM face_generation_age_ranges
        WHERE code = $1 AND is_active = true
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, code)
            return AgeRangeConfig(**dict(row)) if row else None
    
    async def get_age_ranges_for_use_case(self, use_case_code: str) -> List[AgeRangeConfig]:
        """Get age ranges appropriate for specific use case"""
        query = """
        SELECT id, code, display_name, min_age, max_age, prompt_descriptor,
               professional_contexts, is_active
        FROM face_generation_age_ranges
        WHERE is_active = true
        AND $1 = ANY(professional_contexts)
        ORDER BY min_age
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, use_case_code)
            return [AgeRangeConfig(**dict(row)) for row in rows]
    
    # ============================================================================
    # PLATFORM REQUIREMENTS REPOSITORY
    # ============================================================================
    
    async def get_platform_requirements(self) -> List[PlatformRequirementsConfig]:
        """Get all active platform requirements"""
        query = """
        SELECT id, platform_code, display_name, brand_colors, content_guidelines,
               technical_constraints, safe_zones, recommended_formats, api_requirements, is_active
        FROM platform_requirements
        WHERE is_active = true
        ORDER BY platform_code
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [PlatformRequirementsConfig(**dict(row)) for row in rows]
    
    async def get_platform_requirements_by_code(self, platform_code: str) -> Optional[PlatformRequirementsConfig]:
        """Get platform requirements by platform code"""
        query = """
        SELECT id, platform_code, display_name, brand_colors, content_guidelines,
               technical_constraints, safe_zones, recommended_formats, api_requirements, is_active
        FROM platform_requirements
        WHERE platform_code = $1 AND is_active = true
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, platform_code)
            return PlatformRequirementsConfig(**dict(row)) if row else None
    
    # ============================================================================
    # AGGREGATED CONFIGURATION
    # ============================================================================
    
    async def get_complete_creator_config(
        self, 
        language: str = "en",
        platform_filter: Optional[str] = None
    ) -> CreatorPlatformConfig:
        """Get complete configuration for creator platform UI"""
        
        # Get new creator platform configs
        image_formats = await self.get_image_formats()
        use_cases = await self.get_use_cases()
        age_ranges = await self.get_age_ranges()
        creative_variations = await self.get_creative_variations()
        platform_requirements = await self.get_platform_requirements()
        
        # Get existing configs (from original config repo)
        # TODO: Import from existing FaceConfigRepo
        skin_tones = []  # await self.get_skin_tones()
        regions = []     # await self.get_regions()
        styles = []      # await self.get_styles()
        contexts = []    # await self.get_contexts()
        clothing_styles = []  # await self.get_clothing_styles()
        facial_features = []  # await self.get_facial_features()
        
        return CreatorPlatformConfig(
            image_formats=image_formats,
            use_cases=use_cases,
            age_ranges=age_ranges,
            skin_tones=skin_tones,
            regions=regions,
            styles=styles,
            contexts=contexts,
            clothing_styles=clothing_styles,
            creative_variations=creative_variations,
            platform_requirements=platform_requirements,
            facial_features=facial_features
        )
    
    # ============================================================================
    # VALIDATION HELPERS
    # ============================================================================
    
    async def validate_request_config(
        self, 
        image_format_code: str,
        use_case_code: str,
        age_range_code: str,
        platform_code: Optional[str] = None
    ) -> Dict[str, bool]:
        """Validate that all request configuration codes exist and are compatible"""
        
        results = {}
        
        # Validate image format
        image_format = await self.get_image_format_by_code(image_format_code)
        results['image_format_valid'] = image_format is not None
        
        # Validate use case
        use_case = await self.get_use_case_by_code(use_case_code)
        results['use_case_valid'] = use_case is not None
        
        # Validate age range
        age_range = await self.get_age_range_by_code(age_range_code)
        results['age_range_valid'] = age_range is not None
        
        # Check compatibility between use case and image format
        if use_case and image_format:
            results['format_use_case_compatible'] = image_format_code in use_case.recommended_formats
        else:
            results['format_use_case_compatible'] = False
        
        # Check platform compatibility if specified
        if platform_code:
            platform_req = await self.get_platform_requirements_by_code(platform_code)
            results['platform_valid'] = platform_req is not None
            
            if platform_req and image_format:
                results['platform_format_compatible'] = image_format_code in platform_req.recommended_formats
            else:
                results['platform_format_compatible'] = False
        
        return results
    
    async def get_recommended_config(
        self, 
        use_case_code: str,
        platform_code: Optional[str] = None
    ) -> Dict[str, List[str]]:
        """Get recommended configurations for a use case and platform"""
        
        recommendations = {}
        
        # Get use case
        use_case = await self.get_use_case_by_code(use_case_code)
        if use_case:
            recommendations['image_formats'] = use_case.recommended_formats
            
        # Get platform requirements if specified
        if platform_code:
            platform_req = await self.get_platform_requirements_by_code(platform_code)
            if platform_req:
                # Intersect use case formats with platform formats
                if 'image_formats' in recommendations:
                    recommendations['image_formats'] = [
                        fmt for fmt in recommendations['image_formats'] 
                        if fmt in platform_req.recommended_formats
                    ]
                else:
                    recommendations['image_formats'] = platform_req.recommended_formats
        
        # Get compatible age ranges
        age_ranges = await self.get_age_ranges_for_use_case(use_case_code)
        recommendations['age_ranges'] = [ar.code for ar in age_ranges]
        
        # Get compatible creative variations
        variations = await self.get_compatible_variations(use_case_code)
        recommendations['creative_variations'] = {
            var_type: [v.code for v in vars_list]
            for var_type, vars_list in variations.items()
        }
        
        return recommendations