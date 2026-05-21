from __future__ import annotations

from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

from app.domain.enums import (
    LongformMode,
    LongformStage,
    QcDecision,
    RenderRoute,
    ScenarioType,
    ShotType,
)

# -------------------------
# Helpers
# -------------------------

def _strip_or_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_camera_angle(value: Optional[str]) -> Optional[str]:
    value = _strip_or_none(value)
    if value is None:
        return None
    return {
        "low_angle": "slight_low",
        "high_angle": "slight_high",
    }.get(value, value)


def _normalize_camera_framing(value: Optional[str]) -> Optional[str]:
    value = _strip_or_none(value)
    if value is None:
        return None
    return {
        "medium_shot": "medium",
        "wide_shot": "medium_wide",
    }.get(value, value)


def _normalize_camera_motion_style(value: Optional[str]) -> Optional[str]:
    value = _strip_or_none(value)
    if value is None:
        return None
    return {
        "steady": "static",
        "slow_push_in": "gentle_push_in",
        "gentle_parallax": "subtle_drift",
    }.get(value, value)


def _normalize_background_mode(value: Optional[str]) -> Optional[str]:
    value = _strip_or_none(value)
    if value is None:
        return None
    return {
        "static": "fixed",
        "dynamic": "movement_based",
    }.get(value, value)

LongformProfile = Literal["talking_video", "cinematic_video_direction"]
QualityTier = Literal["economy", "premium"]
CameraAngle = Literal["frontal", "eye_level", "slight_low", "slight_high", "three_quarter_left", "three_quarter_right", "low_angle", "high_angle"]
CameraFraming = Literal["close_up", "medium_close_up", "medium", "medium_wide", "medium_shot", "wide_shot"]
CameraMotionStyle = Literal["static", "gentle_push_in", "gentle_pull_out", "subtle_drift", "locked_off", "steady", "slow_push_in", "gentle_parallax"]
TalkingBackgroundMode = Literal["fixed", "movement_based", "static", "dynamic"]


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

    # existing addition
    gender: Optional[Literal["male", "female"]] = None


class PricingConfirmationInput(BaseModel):
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None


class CreateLongformRequest(BaseModel):
    """
    Keep this strict for existing E2E/back-compat callers that already send:
      - image_ref
      - script
      - voice
    New cinematic/director fields are additive and optional.
    """
    image_ref: str
    script: str
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    # IMPORTANT: svc-fusion duration_sec max=120
    segment_seconds: int = Field(default=60, ge=1, le=120)
    max_segment_seconds: int = Field(default=120, ge=1, le=120)

    output_resolution: str = "1080p"  # "720p"|"1080p"|"4k"

    # NEW: safe additive fields for directed mode
    mode: LongformMode = LongformMode.legacy
    longform_profile: LongformProfile = "talking_video"
    quality_tier: QualityTier = "premium"
    provider_hint: Optional[str] = None
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "9:16"
    camera_angle: Optional[CameraAngle] = None
    camera_framing: Optional[CameraFraming] = None
    camera_motion_style: Optional[CameraMotionStyle] = None
    goal: Optional[str] = None
    audience: Optional[str] = None
    tone: List[str] = Field(default_factory=list)
    style: List[str] = Field(default_factory=list)
    scenario_type: ScenarioType = ScenarioType.auto
    cta: Optional[str] = None
    image_urls: List[str] = Field(default_factory=list)
    video_urls: List[str] = Field(default_factory=list)
    screenshot_urls: List[str] = Field(default_factory=list)
    logo_url: Optional[str] = None
    external_provider_ok: bool = False
    require_subtitles: bool = True
    max_repair_rounds: int = Field(default=1, ge=0, le=3)
    background_mode: Optional[TalkingBackgroundMode] = None
    tags: Dict[str, Any] = Field(default_factory=dict)
    quality_tier: QualityTier = "premium"
    provider_hint: Optional[str] = None
    pricing_confirmation: Optional[PricingConfirmationInput] = None

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
    pricing: Dict[str, Any] = Field(default_factory=dict)
    pricing_summary: Dict[str, Any] = Field(default_factory=dict)


class SegmentView(BaseModel):
    segment_index: int
    status: str
    attempt_count: int = 0
    audio_url: Optional[str] = None
    video_url: Optional[str] = None
    last_error: Optional[str] = None

    # NEW: optional cinematic metadata
    beat_id: Optional[str] = None
    shot_id: Optional[str] = None
    shot_type: Optional[str] = None
    render_route: Optional[str] = None


class LongformStatusResponse(BaseModel):
    id: str
    status: str
    segments_total: int
    segments_done: int
    final_video_url: Optional[str] = None
    segments: List[SegmentView] = Field(default_factory=list)
    last_error: Optional[str] = None
    pricing: Dict[str, Any] = Field(default_factory=dict)
    pricing_summary: Dict[str, Any] = Field(default_factory=dict)

    # NEW: optional richer status
    mode: Optional[str] = None
    stage: Optional[str] = None
    scenario_type: Optional[str] = None
    qc_score: Optional[float] = None
    qc_decision: Optional[str] = None
    background_mode: Optional[Literal["fixed", "movement_based"]] = None
    quality_tier: Optional[QualityTier] = None
    provider_hint: Optional[str] = None


# -------------------------
# NEW: intent/scenario/planning models
# -------------------------

class IntentMessage(BaseModel):
    must_include: List[str] = Field(default_factory=list)
    must_avoid: List[str] = Field(default_factory=list)
    cta: Optional[str] = None


class IntentAssets(BaseModel):
    face_artifact_id: Optional[str] = None
    voice_audio_artifact_id: Optional[str] = None
    logo_url: Optional[str] = None
    image_urls: List[str] = Field(default_factory=list)
    video_urls: List[str] = Field(default_factory=list)
    screenshot_urls: List[str] = Field(default_factory=list)


class IntentConstraints(BaseModel):
    external_provider_ok: bool = False
    require_subtitles: bool = True
    max_repair_rounds: int = Field(default=1, ge=0, le=3)
    aspect_ratios: List[Literal["16:9", "9:16", "1:1"]] = Field(default_factory=lambda: ["9:16"])


class VideoIntent(BaseModel):
    mode: LongformMode = LongformMode.directed
    longform_profile: LongformProfile = "talking_video"
    quality_tier: QualityTier = "premium"
    provider_hint: Optional[str] = None
    goal: str
    audience: Optional[str] = None
    camera_angle: Optional[CameraAngle] = None
    camera_framing: Optional[CameraFraming] = None
    camera_motion_style: Optional[CameraMotionStyle] = None
    tone: List[str] = Field(default_factory=list)
    style: List[str] = Field(default_factory=list)
    scenario_type: ScenarioType = ScenarioType.auto
    duration_sec: int = Field(default=90, ge=1, le=1200)
    message: IntentMessage = Field(default_factory=IntentMessage)
    assets: IntentAssets = Field(default_factory=IntentAssets)
    constraints: IntentConstraints = Field(default_factory=IntentConstraints)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("goal must not be empty")
        return s


class ScenarioPlan(BaseModel):
    scenario_type: ScenarioType
    rationale: str
    target_duration_sec: int
    talking_ratio: float = 0.4
    montage_ratio: float = 0.3
    card_ratio: float = 0.1
    proof_ratio: float = 0.2
    suggested_arc: List[str] = Field(default_factory=list)


class StoryBeat(BaseModel):
    beat_id: str
    name: str
    purpose: str
    emotion: Optional[str] = None
    duration_sec: int
    talking_priority: float = 0.5
    visual_direction: Optional[str] = None
    message_points: List[str] = Field(default_factory=list)


class ScriptSpec(BaseModel):
    spoken_text: Optional[str] = None
    onscreen_text: List[str] = Field(default_factory=list)
    subtitle_text: Optional[str] = None
    voiceover_text: Optional[str] = None


class ShotSpec(BaseModel):
    shot_id: str
    beat_id: str
    shot_index: int
    shot_type: ShotType
    render_route: RenderRoute
    duration_sec: int
    title: Optional[str] = None
    script: ScriptSpec = Field(default_factory=ScriptSpec)
    visual_brief: Optional[str] = None
    asset_requirements: Dict[str, Any] = Field(default_factory=dict)
    resolved_assets: Dict[str, Any] = Field(default_factory=dict)
    transition_in: Optional[str] = None
    transition_out: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class TimelineManifest(BaseModel):
    version: int = 1
    project_id: str
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "9:16"
    shots: List[ShotSpec] = Field(default_factory=list)
    subtitle_track_url: Optional[str] = None
    music_track_url: Optional[str] = None
    overlay_meta: Dict[str, Any] = Field(default_factory=dict)
    export_meta: Dict[str, Any] = Field(default_factory=dict)


class QcIssue(BaseModel):
    code: str
    severity: Literal["low", "medium", "high"]
    message: str
    beat_id: Optional[str] = None
    shot_id: Optional[str] = None


class QcResult(BaseModel):
    decision: QcDecision
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    issues: List[QcIssue] = Field(default_factory=list)
    recommended_repairs: List[Dict[str, Any]] = Field(default_factory=list)


# -------------------------
# NEW: flexible directed intent block for new API requests
# -------------------------

class DirectedIntentInput(BaseModel):
    """
    Lightweight request-time intent block.
    The service can transform this into VideoIntent internally.
    """
    goal: str
    audience: Optional[str] = None
    longform_profile: LongformProfile = "talking_video"
    quality_tier: QualityTier = "premium"
    provider_hint: Optional[str] = None
    camera_angle: Optional[CameraAngle] = None
    camera_framing: Optional[CameraFraming] = None
    camera_motion_style: Optional[CameraMotionStyle] = None
    tone: List[str] = Field(default_factory=list)
    style: List[str] = Field(default_factory=list)
    scenario_type: ScenarioType = ScenarioType.auto
    duration_sec: int = Field(default=90, ge=1, le=1200)

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("intent.goal must not be empty")
        return s

    @field_validator("camera_angle", mode="before")
    @classmethod
    def _normalize_camera_angle_field(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_camera_angle(v)

    @field_validator("camera_framing", mode="before")
    @classmethod
    def _normalize_camera_framing_field(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_camera_framing(v)

    @field_validator("camera_motion_style", mode="before")
    @classmethod
    def _normalize_camera_motion_style_field(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_camera_motion_style(v)


# -------------------------
# Compatibility models expected by longform.py
# KEPT, but now extended for both legacy and directed modes
# -------------------------

class LongformCreateRequest(BaseModel):
    """
    Back-compat wrapper so longform.py can keep its imports.

    Accepts both old and new naming:
      - image_ref / face_artifact_id
      - script / script_text
      - voice / voice_cfg

    Also supports the new directed mode via `intent`.
    """
    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=False,
        extra="ignore",
    )

    # Back-compat / legacy inputs
    face_artifact_id: Optional[str] = Field(default=None, alias="image_ref")
    script_text: Optional[str] = Field(default=None, alias="script")
    voice_cfg: VoiceConfig = Field(default_factory=VoiceConfig, alias="voice")

    # Existing options
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "9:16"
    longform_profile: LongformProfile = "talking_video"
    quality_tier: QualityTier = "premium"
    provider_hint: Optional[str] = None
    camera_angle: Optional[CameraAngle] = None
    camera_framing: Optional[CameraFraming] = None
    camera_motion_style: Optional[CameraMotionStyle] = None
    background_mode: Optional[TalkingBackgroundMode] = None
    segment_seconds: int = Field(default=60, ge=1, le=120)
    max_segment_seconds: int = Field(default=120, ge=1, le=120)
    tags: Dict[str, Any] = Field(default_factory=dict)

    # Existing gender policy
    voice_gender_mode: Optional[Literal["auto", "manual"]] = "auto"
    voice_gender: Optional[Literal["male", "female"]] = None

    # NEW: request mode
    mode: LongformMode = LongformMode.legacy

    # NEW: richer directed request blocks
    intent: Optional[DirectedIntentInput] = None
    message: IntentMessage = Field(default_factory=IntentMessage)
    assets: IntentAssets = Field(default_factory=IntentAssets)
    constraints: IntentConstraints = Field(default_factory=IntentConstraints)

    # NEW: optional top-level convenience fields
    goal: Optional[str] = None
    audience: Optional[str] = None
    tone: List[str] = Field(default_factory=list)
    style: List[str] = Field(default_factory=list)
    scenario_type: ScenarioType = ScenarioType.auto
    cta: Optional[str] = None
    external_provider_ok: Optional[bool] = None
    require_subtitles: Optional[bool] = None
    max_repair_rounds: Optional[int] = Field(default=None, ge=0, le=3)
    logo_url: Optional[str] = None
    image_urls: List[str] = Field(default_factory=list)
    video_urls: List[str] = Field(default_factory=list)
    screenshot_urls: List[str] = Field(default_factory=list)

    @field_validator("camera_angle", mode="before")
    @classmethod
    def _normalize_camera_angle_field(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_camera_angle(v)

    @field_validator("camera_framing", mode="before")
    @classmethod
    def _normalize_camera_framing_field(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_camera_framing(v)

    @field_validator("camera_motion_style", mode="before")
    @classmethod
    def _normalize_camera_motion_style_field(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_camera_motion_style(v)

    @field_validator("background_mode", mode="before")
    @classmethod
    def _normalize_background_mode_field(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_background_mode(v)

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> "LongformCreateRequest":
        self.face_artifact_id = _strip_or_none(self.face_artifact_id)
        self.script_text = _strip_or_none(self.script_text)
        self.goal = _strip_or_none(self.goal)
        self.cta = _strip_or_none(self.cta)
        self.provider_hint = _strip_or_none(self.provider_hint)
        self.camera_angle = _normalize_camera_angle(self.camera_angle)
        self.camera_framing = _normalize_camera_framing(self.camera_framing)
        self.camera_motion_style = _normalize_camera_motion_style(self.camera_motion_style)
        self.background_mode = _normalize_background_mode(self.background_mode)

        # Apply top-level convenience overrides into nested blocks if present
        if self.goal and self.intent is None:
            self.intent = DirectedIntentInput(
                goal=self.goal,
                audience=self.audience,
                longform_profile=self.longform_profile,
                quality_tier=self.quality_tier,
                provider_hint=self.provider_hint,
                camera_angle=self.camera_angle,
                camera_framing=self.camera_framing,
                camera_motion_style=self.camera_motion_style,
                tone=self.tone,
                style=self.style,
                scenario_type=self.scenario_type,
                duration_sec=max(self.segment_seconds, 1),
            )

        if self.cta and not self.message.cta:
            self.message.cta = self.cta

        if self.external_provider_ok is not None:
            self.constraints.external_provider_ok = self.external_provider_ok
        if self.require_subtitles is not None:
            self.constraints.require_subtitles = self.require_subtitles
        if self.max_repair_rounds is not None:
            self.constraints.max_repair_rounds = self.max_repair_rounds

        if self.logo_url and not self.assets.logo_url:
            self.assets.logo_url = self.logo_url
        if self.image_urls and not self.assets.image_urls:
            self.assets.image_urls = self.image_urls
        if self.video_urls and not self.assets.video_urls:
            self.assets.video_urls = self.video_urls
        if self.screenshot_urls and not self.assets.screenshot_urls:
            self.assets.screenshot_urls = self.screenshot_urls
        if self.face_artifact_id and not self.assets.face_artifact_id:
            self.assets.face_artifact_id = self.face_artifact_id

        if not self.provider_hint and self.quality_tier == "economy" and self.longform_profile == "talking_video":
            self.provider_hint = "veed_fabric"

        if self.mode == LongformMode.directed and self.quality_tier != "economy":
            self.background_mode = "movement_based"
        elif self.background_mode is None:
            self.background_mode = "fixed"

        # Validation policy:
        # 1) legacy mode requires face_artifact_id + script_text
        # 2) directed mode requires either intent.goal or script_text
        if self.mode == LongformMode.legacy:
            if not self.face_artifact_id:
                raise ValueError("face_artifact_id/image_ref is required in legacy mode")
            if not self.script_text:
                raise ValueError("script_text/script is required in legacy mode")
        else:
            if not self.intent and not self.script_text:
                raise ValueError("directed mode requires either intent.goal or script_text")

        return self


class LongformJobCreated(BaseModel):
    job_id: str
    status: str = "queued"
    pricing: Dict[str, Any] = Field(default_factory=dict)
    pricing_summary: Dict[str, Any] = Field(default_factory=dict)

    # NEW
    mode: Optional[str] = None
    stage: Optional[str] = None
    scenario_type: Optional[str] = None
    longform_profile: Optional[LongformProfile] = None
    background_mode: Optional[Literal["fixed", "movement_based"]] = None
    quality_tier: Optional[QualityTier] = None
    provider_hint: Optional[str] = None
    camera_angle: Optional[CameraAngle] = None
    camera_framing: Optional[CameraFraming] = None
    camera_motion_style: Optional[CameraMotionStyle] = None


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

    # NEW: shot-level metadata
    beat_id: Optional[str] = None
    shot_id: Optional[str] = None
    shot_type: Optional[str] = None
    render_route: Optional[str] = None
    title: Optional[str] = None


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

    # Existing optional echoes
    voice_gender_mode: Optional[Literal["auto", "manual"]] = None
    voice_gender: Optional[Literal["male", "female"]] = None

    # NEW: richer orchestration visibility
    mode: Optional[str] = None
    stage: Optional[str] = None
    scenario_type: Optional[str] = None
    goal: Optional[str] = None
    audience: Optional[str] = None
    tone: List[str] = Field(default_factory=list)
    style: List[str] = Field(default_factory=list)
    longform_profile: Optional[LongformProfile] = None
    quality_tier: Optional[QualityTier] = None
    provider_hint: Optional[str] = None
    camera_angle: Optional[CameraAngle] = None
    camera_framing: Optional[CameraFraming] = None
    camera_motion_style: Optional[CameraMotionStyle] = None
    background_mode: Optional[Literal["fixed", "movement_based"]] = None

    # NEW: planning payloads
    story_beats: List[StoryBeat] = Field(default_factory=list)
    shots: List[LongformSegmentView] = Field(default_factory=list)
    timeline: Optional[TimelineManifest] = None

    # NEW: QC
    qc_score: Optional[float] = None
    qc_decision: Optional[str] = None
    qc: Optional[QcResult] = None
    pricing: Dict[str, Any] = Field(default_factory=dict)
    pricing_summary: Dict[str, Any] = Field(default_factory=dict)


# -------------------------
# Optional internal aggregate state model
# Helpful for orchestrator / worker, not required by API.
# -------------------------

class LongformProjectState(BaseModel):
    job_id: str
    user_id: str
    status: str
    stage: LongformStage = LongformStage.queued
    mode: LongformMode = LongformMode.legacy

    quality_tier: Optional[QualityTier] = None
    provider_hint: Optional[str] = None
    intent: Optional[VideoIntent] = None
    scenario: Optional[ScenarioPlan] = None
    story_beats: List[StoryBeat] = Field(default_factory=list)
    shots: List[ShotSpec] = Field(default_factory=list)
    timeline: Optional[TimelineManifest] = None
    qc: Optional[QcResult] = None

    repair_round: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)