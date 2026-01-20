# services/svc-face/app/app/domain/models.py
from __future__ import annotations
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, HttpUrl
from app.domain.enums import FaceGenerationMode, Gender, SupportedLanguage

class FaceGenerateRequest(BaseModel):
    """Request to generate faces"""
    mode: FaceGenerationMode
    language: SupportedLanguage = SupportedLanguage.ENGLISH
    
    # Core parameters
    gender: Gender
    age_group: str  # Fetched from DB
    region: str  # Fetched from DB
    style: str  # Fetched from DB
    context: Optional[str] = None  # Socioeconomic context from DB
    
    # Optional customization
    user_prompt: Optional[str] = None  # User's custom text (any language)
    num_variants: int = Field(default=4, ge=1, le=4)
    
    # Image-to-image mode only
    source_image_url: Optional[HttpUrl] = None
    modifications: Optional[Dict[str, Any]] = None
    preservation_strength: float = Field(default=0.3, ge=0.1, le=0.5)

class FaceProfileView(BaseModel):
    """Single face profile response"""
    face_profile_id: str
    image_url: str
    thumbnail_url: Optional[str] = None
    variant: int
    generation_params: Dict[str, Any]
    
class FaceJobView(BaseModel):
    """Face generation job response"""
    job_id: str
    status: str
    faces: List[FaceProfileView] = []
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    
class RegionConfigView(BaseModel):
    """Region configuration view"""
    code: str
    display_name: str
    sub_region: Optional[str]
    is_active: bool
    
class StyleConfigView(BaseModel):
    """Style configuration view"""
    code: str
    display_name: str
    category: str
    is_active: bool

class ContextConfigView(BaseModel):
    """Context configuration view"""
    code: str
    display_name: str
    economic_class: str
    glamour_level: int
    is_active: bool