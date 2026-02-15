from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field, ConfigDict, model_validator

# ============================================================================
# ENUMS
# ============================================================================


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"


class FaceGenerationMode(str, Enum):
    TEXT_TO_IMAGE = "text-to-image"
    IMAGE_TO_IMAGE = "image-to-image"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ============================================================================
# LEGACY COMPATIBILITY MODELS (for existing routes)
# ============================================================================
class FaceGenerateRequest(BaseModel):
    """Legacy model for backward compatibility with /api/face/generate"""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(..., max_length=1500)
    negative_prompt: Optional[str] = None
    num_images: int = Field(default=1, ge=1, le=4)
    language: str = "en"
    mode: FaceGenerationMode = FaceGenerationMode.TEXT_TO_IMAGE

    width: int = Field(default=1024, ge=512, le=2048)
    height: int = Field(default=1024, ge=512, le=2048)

    guidance_scale: float = Field(default=3.5, ge=1.0, le=20.0)
    num_inference_steps: int = Field(default=28, ge=10, le=50)

    seed_mode: Literal["auto", "random", "deterministic"] = "auto"
    seed: Optional[int] = None


class FaceProfileView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    face_profile_id: str
    image_url: str
    thumbnail_url: Optional[str] = None
    variant: int = 0
    generation_params: Dict[str, Any] = Field(default_factory=dict)


class FaceJobView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    status: str
    faces: List[FaceProfileView] = Field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class RegionConfigView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    display_name: str
    sub_region: Optional[str] = None
    is_active: bool = True


class ContextConfigView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    display_name: str
    economic_class: Optional[str] = None
    glamour_level: Optional[int] = None
    is_active: bool = True


class StyleConfigView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    display_name: str
    category: Optional[str] = None
    is_active: bool = True


# ============================================================================
# CREATOR PLATFORM (Phase-1: single_person / two_people)
# ============================================================================


class SubjectSpec(BaseModel):
    """
    One subject in the frame.
    Phase-1: gender + optional relationship role hints.
    """
    model_config = ConfigDict(extra="ignore")

    gender: Optional[Gender] = None
    relationship_role: Optional[str] = None  # e.g., "partner", "friend", "colleague"


class CreatorPlatformRequest(BaseModel):
    """
    Creator platform face generation request.

    Phase-1:
      - subject_composition_code: "single_person" | "two_people"
      - gender: optional hint for single_person
      - subjects: optional list (lets UI specify M+M, M+F, F+F)

    I2I support:
      - Old path: source_image_url
      - New path: source_image_asset_id (returned by /api/face/assets/upload)
        If source_image_asset_id is present and source_image_url is missing,
        we automatically mirror asset_id into source_image_url so older code paths work.
    """
    model_config = ConfigDict(extra="ignore")

    # Core
    mode: FaceGenerationMode = FaceGenerationMode.TEXT_TO_IMAGE
    language: str = "en"

    # Demographics (optional)
    age_range_code: Optional[str] = None
    skin_tone_code: Optional[str] = None
    region_code: Optional[str] = None

    # ✅ Composition (Phase-1)
    subject_composition_code: Literal["single_person", "two_people"] = "single_person"

    # Optional single-person hint (UI can set)
    gender: Optional[Gender] = None

    # Optional explicit subject list (supports M+M, M+F, F+F)
    subjects: Optional[List[SubjectSpec]] = None

    # Creator config codes
    image_format_code: Optional[str] = None
    use_case_code: Optional[str] = None
    style_code: Optional[str] = None
    context_code: Optional[str] = None
    clothing_style_code: Optional[str] = None
    platform_code: Optional[str] = None

    # Generation control
    num_variants: int = Field(default=4, ge=1, le=8)
    user_prompt: Optional[str] = Field(default=None, max_length=1500)

    # Seeding
    seed_mode: Literal["auto", "random", "deterministic"] = "auto"
    seed: Optional[int] = None
    request_nonce: Optional[str] = None

    # I2I (old + new)
    source_image_url: Optional[str] = None
    source_image_asset_id: Optional[str] = None

    preservation_strength: float = Field(0.75, ge=0.0, le=1.0)
    # higher = preserve identity more (minimal change)

    # Future-proof knobs
    facial_features: Dict[str, str] = Field(default_factory=dict)
    preferred_variations: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_subjects(self):
        """
        Keep request permissive (no 422s) and normalize defaults:
          - single_person: if subjects missing, synthesize 1 subject from gender (if provided)
          - two_people: if subjects missing, synthesize 2 subjects with unknown genders
        Prompt engine can decide details when genders are unknown.
        """
        if self.subject_composition_code == "single_person":
            if not self.subjects:
                self.subjects = [SubjectSpec(gender=self.gender)]
            if self.gender is None and self.subjects and self.subjects[0].gender is not None:
                self.gender = self.subjects[0].gender

        if self.subject_composition_code == "two_people":
            if not self.subjects:
                self.subjects = [SubjectSpec(), SubjectSpec()]
            if len(self.subjects) == 1:
                self.subjects = [self.subjects[0], SubjectSpec()]

        # Bridge: if new field provided, mirror into source_image_url for older code paths.
        if (not (self.source_image_url or "").strip()) and (self.source_image_asset_id or "").strip():
            self.source_image_url = (self.source_image_asset_id or "").strip()

        return self


# ============================================================================
# RESPONSES (creator platform)
# ============================================================================


class JobCreatedResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    status: str
    message: str
    estimated_completion_time: str
    config: Dict[str, Any]


class GeneratedVariant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    variant_number: int
    face_profile_id: str
    media_asset_id: str
    image_url: str
    prompt_used: str
    technical_specs: Dict[str, Any]
    creative_variations: Dict[str, Any]


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    status: JobStatus
    message: str
    progress: Optional[Dict[str, Any]] = None
    variants: Optional[List[GeneratedVariant]] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CreatorConfigResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image_formats: List[Dict[str, Any]]
    use_cases: List[Dict[str, Any]]
    age_ranges: List[Dict[str, Any]]
    regions: List[Dict[str, Any]]
    skin_tones: List[Dict[str, Any]]


# ============================================================================
# DB TABLE MODELS
# ============================================================================


class StudioJobDB(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    studio_type: str
    status: str
    user_id: str
    request_hash: str
    payload_json: Dict[str, Any]
    meta_json: Dict[str, Any]
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    attempt_count: int = 0
    next_run_at: datetime


class FaceProfileDB(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    user_id: str
    display_name: Optional[str]
    primary_image_asset_id: str
    attributes_json: Dict[str, Any]
    meta_json: Dict[str, Any]
    status: str = "active"
    created_at: datetime
    updated_at: datetime


class MediaAssetDB(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    user_id: str
    kind: str
    storage_ref: str
    content_type: str
    bytes: int
    meta_json: Dict[str, Any]
    created_at: datetime
    updated_at: datetime