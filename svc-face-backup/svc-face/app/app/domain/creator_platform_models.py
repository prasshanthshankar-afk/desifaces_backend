# services/svc-face/app/domain/creator_platform_models.py
from __future__ import annotations
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field, validator
from enum import Enum

class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    NEUTRAL = "neutral"

class FaceGenerationMode(str, Enum):
    TEXT_TO_IMAGE = "text-to-image"
    IMAGE_TO_IMAGE = "image-to-image"

class SupportedLanguage(str, Enum):
    ENGLISH = "en"
    HINDI = "hi"
    TAMIL = "ta"
    TELUGU = "te"
    MALAYALAM = "ml"
    KANNADA = "kn"
    GUJARATI = "gu"
    MARATHI = "mr"
    BENGALI = "bn"
    PUNJABI = "pa"

# ============================================================================
# CREATOR PLATFORM REQUEST MODEL - Tightly coupled to database schema
# ============================================================================
class CreatorPlatformFaceRequest(BaseModel):
    """
    Enhanced face generation request for creator platform.
    All fields map directly to database configuration tables.
    """
    
    # Core generation parameters
    mode: FaceGenerationMode
    language: SupportedLanguage = SupportedLanguage.ENGLISH
    
    # User-controlled demographics (fixed across variants)
    # Maps to: face_generation_age_ranges table
    age_range_code: str = Field(..., description="Code from face_generation_age_ranges table")
    
    # Maps to: face_generation_skin_tones table  
    skin_tone_code: str = Field(..., description="Code from face_generation_skin_tones table")
    
    # Maps to: face_generation_regions table
    region_code: str = Field(..., description="Code from face_generation_regions table")
    
    gender: Gender
    
    # Optional specific facial features (maps to face_generation_features table)
    facial_features: Optional[Dict[str, str]] = Field(
        default=None,
        description="Feature codes from face_generation_features table grouped by feature_type"
    )
    
    # Creator platform parameters (database-driven)
    # Maps to: face_generation_image_formats table
    image_format_code: str = Field(..., description="Code from face_generation_image_formats table")
    
    # Maps to: face_generation_use_cases table
    use_case_code: str = Field(..., description="Code from face_generation_use_cases table") 
    
    # Maps to: face_generation_styles table (existing)
    style_code: str = Field(..., description="Code from face_generation_styles table")
    
    # Optional enhancements (all database-driven)
    # Maps to: face_generation_contexts table
    context_code: Optional[str] = Field(default=None, description="Code from face_generation_contexts table")
    
    # Maps to: face_generation_clothing_styles table  
    clothing_style_code: Optional[str] = Field(default=None, description="Code from face_generation_clothing_styles table")
    
    # Maps to: platform_requirements table
    platform_code: Optional[str] = Field(default=None, description="Code from platform_requirements table")
    
    # Generation control
    num_variants: int = Field(default=4, ge=1, le=8, description="Number of creative variants to generate")
    
    # User input
    user_prompt: Optional[str] = Field(default=None, max_length=500, description="User's custom prompt additions")
    
    # Image-to-image specific (when mode = "image-to-image")
    source_image_url: Optional[str] = Field(default=None, description="Source image URL for i2i generation")
    preservation_strength: float = Field(default=0.3, ge=0.1, le=0.5, description="Face preservation strength for i2i")
    
    # Advanced customization (maps to face_generation_variations table)
    preferred_variations: Optional[List[str]] = Field(
        default=None,
        description="Preferred variation codes from face_generation_variations table"
    )
    
    # Platform optimization
    platform_requirements: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Platform-specific requirements (auto-filled from platform_code)"
    )
    
    @validator('facial_features')
    def validate_facial_features_structure(cls, v):
        """Validate facial features structure matches database schema"""
        if v is not None:
            valid_feature_types = {
                'jaw', 'nose', 'eyes', 'lips', 'cheekbones', 'hair', 'body'
            }
            for feature_type in v.keys():
                if feature_type not in valid_feature_types:
                    raise ValueError(f"Invalid feature_type: {feature_type}")
        return v

# ============================================================================
# DATABASE CONFIGURATION MODELS - Match database table structures exactly
# ============================================================================

class ImageFormatConfig(BaseModel):
    """Maps to face_generation_image_formats table"""
    id: str
    code: str
    display_name: Dict[str, str]
    width: int
    height: int
    aspect_ratio: str
    platform_category: str
    recommended_platforms: List[str]
    technical_specs: Dict[str, Any]
    safe_zones: Dict[str, int]
    is_active: bool
    sort_order: int

class UseCaseConfig(BaseModel):
    """Maps to face_generation_use_cases table"""
    id: str
    code: str
    display_name: Dict[str, str]
    category: str
    description: Optional[Dict[str, str]]
    prompt_base: str
    lighting_style: Optional[str]
    composition_style: Optional[str]
    mood_descriptors: Optional[str]
    background_type: Optional[str]
    recommended_formats: List[str]
    target_audience: Optional[str]
    industry_focus: List[str]
    is_active: bool
    sort_order: int

class CreativeVariationConfig(BaseModel):
    """Maps to face_generation_variations table"""
    id: str
    variation_type: str
    code: str
    display_name: Dict[str, str]
    prompt_modifier: str
    use_case_compatibility: List[str]
    mood_impact: Optional[str]
    professional_level: int
    creativity_level: int
    is_active: bool

class AgeRangeConfig(BaseModel):
    """Maps to face_generation_age_ranges table"""
    id: str
    code: str
    display_name: Dict[str, str]
    min_age: int
    max_age: int
    prompt_descriptor: str
    professional_contexts: List[str]
    is_active: bool

class PlatformRequirementsConfig(BaseModel):
    """Maps to platform_requirements table"""
    id: str
    platform_code: str
    display_name: Dict[str, str]
    brand_colors: Dict[str, str]
    content_guidelines: Dict[str, Any]
    technical_constraints: Dict[str, Any]
    safe_zones: Dict[str, int]
    recommended_formats: List[str]
    api_requirements: Dict[str, Any]
    is_active: bool

# ============================================================================
# GENERATION RESPONSE MODELS
# ============================================================================

class CreatedVariant(BaseModel):
    """Individual variant result"""
    variant_number: int
    face_profile_id: str
    media_asset_id: str
    storage_path: str
    blob_url: str
    prompt_used: str
    variation_applied: Dict[str, str]  # Which variations were used
    technical_specs: Dict[str, Any]   # Actual image specs

class CreatorPlatformGenerationResult(BaseModel):
    """Complete generation result for creator platform"""
    job_id: str
    user_id: str
    request_config: CreatorPlatformFaceRequest
    image_format_used: ImageFormatConfig
    use_case_used: UseCaseConfig
    variants_created: List[CreatedVariant]
    total_variants: int
    generation_metadata: Dict[str, Any]
    platform_optimization: Optional[Dict[str, Any]]
    created_at: str
    status: str

# ============================================================================
# CONFIG AGGREGATION MODEL
# ============================================================================

class CreatorPlatformConfig(BaseModel):
    """Aggregated configuration for UI and generation"""
    image_formats: List[ImageFormatConfig]
    use_cases: List[UseCaseConfig]
    age_ranges: List[AgeRangeConfig]
    skin_tones: List[Dict[str, Any]]  # From existing table
    regions: List[Dict[str, Any]]     # From existing table
    styles: List[Dict[str, Any]]      # From existing table
    contexts: List[Dict[str, Any]]    # From existing table
    clothing_styles: List[Dict[str, Any]]  # From existing table
    creative_variations: List[CreativeVariationConfig]
    platform_requirements: List[PlatformRequirementsConfig]
    facial_features: List[Dict[str, Any]]  # From existing table, grouped by feature_type
    
    class Config:
        """Pydantic config for performance"""
        json_encoders = {
            # Add any custom encoders if needed
        }

# ============================================================================
# REQUEST VALIDATION AND ENHANCEMENT
# ============================================================================

class ValidatedCreatorRequest(BaseModel):
    """Request after validation and database lookups"""
    original_request: CreatorPlatformFaceRequest
    resolved_config: Dict[str, Any]  # All database config resolved
    generation_plan: Dict[str, Any]  # How variants will be generated
    validation_results: Dict[str, bool]  # What validations passed
    
    @classmethod
    def from_request(cls, request: CreatorPlatformFaceRequest, config_repo) -> 'ValidatedCreatorRequest':
        """Create validated request by resolving all database references"""
        # This will be implemented in the service layer
        pass