from __future__ import annotations

from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator

# -------------------------
# Your existing models (kept + extended safely)
# -------------------------

class VoiceConfig(BaseModel):
    locale: str = Field(default="en-US")
    voice_id: Optional[str] = None
    speaking_rate: Optional[float] = None

    # extra fields (safe)
    voice: Optional[str] = None
    translate: bool = False
    output_format: Literal["mp3", "wav"] = "mp3"

    # ✅ NEW (optional): voice gender hint (used only if your worker chooses to)
    gender: Optional[Literal["male", "female"]] = None


class CreateLongformRequest(BaseModel):
    image_ref: str
    script: str
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    # IMPORTANT: svc-fusion duration_sec max=120
    segment_seconds: int = Field(default=60, ge=1, le=120)
    max_segment_seconds: int = Field(default=120, ge=1, le=120)

    output_resolution: str = "1080p"  # "720p"|"1080p"|"4k"

    @field_validator("script")
    @classmethod
    def _strip_script(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("script must not be empty")
        return s


class CreateLongformResponse(BaseModel):
    longform_job_id: str
    segments_planned: int
    status: str = "queued"


class SegmentView(BaseModel):
    segment_index: int
    status: str
    attempt_count: int = 0
    audio_url: Optional[str] = None
    video_url: Optional[str] = None
    last_error: Optional[str] = None


class LongformStatusResponse(BaseModel):
    id: str
    status: str
    segments_total: int
    segments_done: int
    final_video_url: Optional[str] = None
    segments: List[SegmentView] = Field(default_factory=list)
    last_error: Optional[str] = None


# -------------------------
# Compatibility models expected by longform.py (ADD/UPDATE THESE)
# -------------------------

class LongformCreateRequest(BaseModel):
    """
    Back-compat wrapper so longform.py can keep its imports.
    Accepts both new names and old names via aliases.
    """
    model_config = ConfigDict(populate_by_name=True)

    # Accept both:
    #  - "face_artifact_id" (new)
    #  - "image_ref" (old)
    face_artifact_id: str = Field(..., alias="image_ref")

    # Accept both:
    #  - "script_text" (new)
    #  - "script" (old)
    script_text: str = Field(..., alias="script")

    # Accept both:
    #  - "voice_cfg" (new)
    #  - "voice" (old)
    voice_cfg: VoiceConfig = Field(default_factory=VoiceConfig, alias="voice")

    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "9:16"
    segment_seconds: int = Field(default=60, ge=1, le=120)
    max_segment_seconds: int = Field(default=120, ge=1, le=120)

    tags: Dict[str, Any] = Field(default_factory=dict)

    # ✅ NEW (optional): voice gender policy (matches DB columns)
    voice_gender_mode: Optional[Literal["auto", "manual"]] = "auto"
    voice_gender: Optional[Literal["male", "female"]] = None

    @field_validator("script_text")
    @classmethod
    def _strip_script_text(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("script_text must not be empty")
        return s


class LongformJobCreated(BaseModel):
    job_id: str
    status: str = "queued"


class LongformSegmentView(BaseModel):
    id: str
    segment_index: int
    status: str
    duration_sec: int

    audio_url: Optional[str] = None
    fusion_job_id: Optional[str] = None
    segment_video_url: Optional[str] = None

    error_code: Optional[str] = None
    error_message: Optional[str] = None


class LongformJobView(BaseModel):
    id: str
    user_id: str
    status: str

    aspect_ratio: str
    segment_seconds: int
    max_segment_seconds: int

    total_segments: int
    completed_segments: int

    final_video_url: Optional[str] = None
    final_storage_path: Optional[str] = None

    error_code: Optional[str] = None
    error_message: Optional[str] = None

    created_at: str
    updated_at: str

    #  NEW (optional): echo back gender policy (so UI can show what’s in effect)
    voice_gender_mode: Optional[Literal["auto", "manual"]] = None
    voice_gender: Optional[Literal["male", "female"]] = None