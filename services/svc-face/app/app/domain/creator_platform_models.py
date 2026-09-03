# domain/creator_platform_models.py
# Database table models for creator platform

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ============================================================================
# CREATOR PLATFORM DATABASE MODELS
# ============================================================================


class ImageFormatDB(BaseModel):
    """Runtime mapping for face_generation_image_formats."""

    id: str
    code: str
    display_name: Dict[str, str]
    width: int
    height: int
    aspect_ratio: str
    platform_category: str
    recommended_platforms: List[str]
    technical_specs: Dict[str, Any]
    safe_zones: Dict[str, Any]
    is_active: bool = True
    sort_order: int = 0
    created_at: datetime


class UseCaseDB(BaseModel):
    """Runtime mapping for face_generation_use_cases.

    The authoritative DB columns mood_descriptors and target_audience are TEXT.
    Older repository conversion code can surface them as list-shaped values; the
    validators normalize that compatibility shape back to the scalar DB contract
    rather than falling back to an untyped dict.
    """

    id: str
    code: str
    display_name: Dict[str, str]
    category: str
    description: Optional[Dict[str, str]] = None
    prompt_base: str
    lighting_style: Optional[str] = None
    composition_style: Optional[str] = None
    mood_descriptors: Optional[str] = None
    background_type: Optional[str] = None
    recommended_formats: List[str] = Field(default_factory=list)
    target_audience: Optional[str] = None
    industry_focus: List[str] = Field(default_factory=list)
    is_active: bool = True
    sort_order: int = 0
    created_at: datetime

    @field_validator("mood_descriptors", "target_audience", mode="before")
    @classmethod
    def _normalize_text_compatibility_shape(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            values = [str(item).strip() for item in value if str(item).strip()]
            return ", ".join(values) if values else None
        return str(value)


class AgeRangeDB(BaseModel):
    """Runtime mapping for face_generation_age_ranges."""

    id: str
    code: str
    display_name: Dict[str, str]
    min_age: int
    max_age: int
    prompt_descriptor: str
    professional_contexts: List[str] = Field(default_factory=list)
    is_active: bool = True


class RegionDB(BaseModel):
    """Runtime mapping for face_generation_regions.

    sub_region/ethnicity_notes/typical_skin_tones are legacy columns. V3 runtime
    retrieval intentionally does not require them for geographic selection, so
    they are optional here. Geography must not be used to infer protected or
    physical characteristics.
    """

    id: str
    code: str
    display_name: Dict[str, str]
    sub_region: Optional[str] = None
    ethnicity_notes: Optional[str] = None
    typical_skin_tones: List[str] = Field(default_factory=list)
    cultural_markers: Optional[Dict[str, Any]] = None
    prompt_base: str
    is_active: bool = True
    sort_order: int = 0
    created_at: Optional[datetime] = None


class SkinToneDB(BaseModel):
    """Runtime mapping for face_generation_skin_tones."""

    id: str
    code: str
    display_name: Dict[str, str]
    hex_reference: Optional[str] = None
    prompt_descriptor: str
    diversity_weight: int = 1
    is_active: bool = True
