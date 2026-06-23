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


class AspectRatio(str, Enum):
    PORTRAIT = "9:16"
    LANDSCAPE = "16:9"
    SQUARE = "1:1"


class ImageSizeHint(str, Enum):
    AUTO = "auto"
    SQUARE_1024 = "1024x1024"
    PORTRAIT_1024x1536 = "1024x1536"
    LANDSCAPE_1536x1024 = "1536x1024"


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

    Framing support:
      - Frontend should send aspect_ratio as the canonical framing control
      - image_size_hint is provider-facing / derived, not something normal users need to pick
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

    # Framing / composition controls
    shot_type_code: Optional[str] = None
    aspect_ratio: AspectRatio = AspectRatio.PORTRAIT

    # Internal/provider-facing image size hint.
    image_size_hint: Optional[ImageSizeHint] = None

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

    preservation_strength: float = Field(0.98, ge=0.0, le=1.0)
    identity_lock: bool = Field(default=False)
    identity_lock_level: Optional[str] = None
    preserve_source_identity: bool = Field(default=False)
    preserve_source_gender: bool = Field(default=False)
    gender_lock_mode: Optional[str] = None
    allowed_i2i_changes: Optional[list[str]] = None
    forbidden_i2i_changes: Optional[list[str]] = None
    identity_lock_instructions: Optional[str] = Field(default=None, max_length=1500)

    # Future-proof knobs
    facial_features: Dict[str, str] = Field(default_factory=dict)
    preferred_variations: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        out = dict(data)

        if not (str(out.get("use_case_code") or "").strip()) and str(out.get("use_case") or "").strip():
            out["use_case_code"] = out.get("use_case")

        if not (str(out.get("shot_type_code") or "").strip()) and str(out.get("shot_type") or "").strip():
            out["shot_type_code"] = out.get("shot_type")

        if not (str(out.get("context_code") or "").strip()) and str(out.get("context") or "").strip():
            out["context_code"] = out.get("context")

        if not (str(out.get("style_code") or "").strip()) and str(out.get("style") or "").strip():
            out["style_code"] = out.get("style")

        raw_ratio = str(out.get("aspect_ratio") or "").strip().lower()
        if raw_ratio in {"portrait", "vertical"}:
            out["aspect_ratio"] = AspectRatio.PORTRAIT.value
        elif raw_ratio in {"landscape", "horizontal"}:
            out["aspect_ratio"] = AspectRatio.LANDSCAPE.value
        elif raw_ratio in {"square"}:
            out["aspect_ratio"] = AspectRatio.SQUARE.value

        if not out.get("image_size_hint") and out.get("size"):
            out["image_size_hint"] = out.get("size")

        return out

    @staticmethod
    def _default_size_for_aspect_ratio(aspect_ratio: AspectRatio) -> ImageSizeHint:
        if aspect_ratio == AspectRatio.SQUARE:
            return ImageSizeHint.SQUARE_1024
        if aspect_ratio == AspectRatio.LANDSCAPE:
            return ImageSizeHint.LANDSCAPE_1536x1024
        return ImageSizeHint.PORTRAIT_1024x1536

    @staticmethod
    def _is_size_compatible_with_aspect_ratio(
        aspect_ratio: AspectRatio,
        image_size_hint: ImageSizeHint,
    ) -> bool:
        if image_size_hint == ImageSizeHint.AUTO:
            return True
        if aspect_ratio == AspectRatio.SQUARE:
            return image_size_hint == ImageSizeHint.SQUARE_1024
        if aspect_ratio == AspectRatio.LANDSCAPE:
            return image_size_hint == ImageSizeHint.LANDSCAPE_1536x1024
        return image_size_hint == ImageSizeHint.PORTRAIT_1024x1536

    @model_validator(mode="after")
    def _normalize_subjects(self):
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

        if (not (self.source_image_url or "").strip()) and (self.source_image_asset_id or "").strip():
            self.source_image_url = (self.source_image_asset_id or "").strip()

        if self.image_size_hint is None:
            self.image_size_hint = self._default_size_for_aspect_ratio(self.aspect_ratio)
        else:
            if not self._is_size_compatible_with_aspect_ratio(self.aspect_ratio, self.image_size_hint):
                raise ValueError(
                    f"image_size_hint {self.image_size_hint.value} is incompatible with aspect_ratio {self.aspect_ratio.value}"
                )

        return self


# ============================================================================
# PRICING MODELS (canonical cross-studio language)
# ============================================================================


class PricingConfirmationModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    quote_id: str
    preview_fingerprint: Optional[str] = None
    user_confirmed: bool = True
    client_presented_amount: Optional[str] = None
    client_presented_currency: Optional[str] = None


class CreatorGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    studio: str = "face"
    studio_input: CreatorPlatformRequest
    pricing_confirmation: Optional[PricingConfirmationModel] = None


class PricingStateView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: str
    enabled: Optional[bool] = None

    quote_id: Optional[str] = None
    quote_expires_at: Optional[str] = None
    preview_fingerprint: Optional[str] = None
    reservation_id: Optional[str] = None
    variant_code: Optional[str] = None

    service_name: Optional[str] = None
    service_action: Optional[str] = None
    sku_code: Optional[str] = None
    unit_type: Optional[str] = None

    estimated_units: Optional[str] = None
    reserved_units: Optional[str] = None
    actual_units: Optional[str] = None
    billed_units: Optional[str] = None
    released_units: Optional[str] = None

    estimated_amount: Optional[str] = None
    final_amount: Optional[str] = None

    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None

    billing_mode: Optional[str] = None
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None
    pricing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    entitlement_reason: Optional[str] = None
    tier_code: Optional[str] = None

    source: Optional[str] = None
    reason: Optional[str] = None
    summary: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingSummaryView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    display_estimate: Optional[str] = None
    display_final: Optional[str] = None
    display_delta: Optional[str] = None
    display_note: Optional[str] = None


class PricingPreviewRequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    studio: str = "face"
    action: str = "generate"
    studio_input: CreatorPlatformRequest
    client_context: Dict[str, Any] = Field(default_factory=dict)


class PricingPreviewResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    studio: str = "face"
    action: str = "generate"

    quote_id: str
    quote_expires_at: Optional[str] = None
    preview_fingerprint: Optional[str] = None

    pricing: PricingStateView
    balance: Dict[str, Any] = Field(default_factory=dict)
    summary: Dict[str, Any] = Field(default_factory=dict)


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

    pricing: Optional[PricingStateView] = None


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

    pricing: Optional[PricingStateView] = None
    pricing_summary: Optional[PricingSummaryView] = None

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
