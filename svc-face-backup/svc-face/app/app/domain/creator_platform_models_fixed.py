# services/svc-face/app/domain/creator_platform_models.py
from __future__ import annotations
from typing import Dict, List, Any, Optional, Union
from pydantic import BaseModel, Field, validator
from enum import Enum
import json
import uuid

class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    NEUTRAL = "neutral"

class ImageFormatConfig(BaseModel):
    """Maps to face_generation_image_formats table"""
    id: str
    code: str
    display_name: Union[Dict[str, str], str]
    width: int
    height: int
    aspect_ratio: str
    platform_category: str
    recommended_platforms: List[str]
    technical_specs: Union[Dict[str, Any], str]
    safe_zones: Union[Dict[str, int], str]
    is_active: bool
    sort_order: int

    @validator('id')
    def convert_uuid_to_string(cls, v):
        if isinstance(v, uuid.UUID):
            return str(v)
        return v

    @validator('display_name')
    def parse_display_name(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except:
                return {"en": v}
        return v

    @validator('technical_specs')
    def parse_technical_specs(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except:
                return {}
        return v

    @validator('safe_zones')
    def parse_safe_zones(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except:
                return {}
        return v

class UseCaseConfig(BaseModel):
    """Maps to face_generation_use_cases table"""
    id: str
    code: str
    display_name: Union[Dict[str, str], str]
    category: str
    description: Optional[Union[Dict[str, str], str]]
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

    @validator('id')
    def convert_uuid_to_string(cls, v):
        if isinstance(v, uuid.UUID):
            return str(v)
        return v

    @validator('display_name', 'description')
    def parse_json_fields(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except:
                return {"en": v} if v else None
        return v

class CreatorPlatformConfig(BaseModel):
    """Aggregated configuration for UI and generation"""
    image_formats: List[ImageFormatConfig]
    use_cases: List[UseCaseConfig]
    # Add other configs as needed

