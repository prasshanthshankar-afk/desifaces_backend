from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.domain.enums import AspectRatio, VoiceMode

FusionProvider = Literal[
    "omnihuman_v15",
    "omnihuman",
    "heygen_av4",
    "kling",
    "luma",
    "runway",
    "native",
    "veed_fabric",
    "veed",
]
DeliverySurface = Literal["instagram_reel", "youtube", "square_social"]
Resolution = Literal["540p", "720p", "1080p"]


def _normalize_provider_name(value: Optional[str]) -> str:
    provider = str(value or "omnihuman_v15").strip().lower()
    if provider in {"veed", "veed_fabric", "fabric", "veed/fabric-1.0"}:
        return "veed_fabric"
    if provider in {"omnihuman", "omnihuman_v15"}:
        return "omnihuman_v15"
    return provider or "omnihuman_v15"


class Consent(BaseModel):
    external_provider_ok: bool = False


class Dimension(BaseModel):
    width: int = Field(ge=64, le=4096)
    height: int = Field(ge=64, le=4096)


class VideoSettings(BaseModel):
    aspect_ratio: AspectRatio = AspectRatio.ar_9_16
    dimension: Optional[Dimension] = None
    duration_sec: Optional[int] = Field(default=None, ge=1, le=600)
    emotion: Optional[str] = Field(default=None, max_length=64)
    motion_style: Optional[str] = Field(default=None, max_length=64)
    resolution: Optional[Resolution] = None
    delivery_surface: Optional[DeliverySurface] = None
    shot_type: Optional[str] = Field(default=None, max_length=64)
    prompt: Optional[str] = Field(default=None, max_length=4000)


class VoiceAudio(BaseModel):
    type: Literal["audio"] = "audio"
    audio_url: Optional[HttpUrl] = None
    audio_asset_id: Optional[str] = None
    audio_artifact_id: Optional[str] = None

    @model_validator(mode="after")
    def at_least_one_source(self) -> "VoiceAudio":
        if self.audio_asset_id is not None and not self.audio_asset_id.strip():
            self.audio_asset_id = None
        if self.audio_artifact_id is not None and not self.audio_artifact_id.strip():
            self.audio_artifact_id = None

        has_url = self.audio_url is not None
        has_asset_id = bool(self.audio_asset_id)
        has_artifact_id = bool(self.audio_artifact_id)

        if not (has_url or has_asset_id or has_artifact_id):
            raise ValueError("voice_audio requires one of: audio_url, audio_asset_id, audio_artifact_id.")

        if self.audio_artifact_id:
            try:
                uuid.UUID(self.audio_artifact_id)
            except Exception:
                raise ValueError("voice_audio.audio_artifact_id must be a valid UUID")
        return self


class VoiceTTS(BaseModel):
    type: Literal["tts"] = "tts"
    voice_id: str = Field(min_length=1)
    script: str = Field(min_length=1, max_length=4000)
    language: Optional[str] = Field(default=None, max_length=64)


class FusionJobCreate(BaseModel):
    face_image_url: Optional[HttpUrl] = None
    face_artifact_id: Optional[str] = None
    heygen_talking_photo_id: Optional[str] = None
    image_key: Optional[str] = None

    voice_mode: VoiceMode = VoiceMode.audio
    voice_audio: Optional[VoiceAudio] = None
    voice_tts: Optional[VoiceTTS] = None

    video: VideoSettings = Field(default_factory=VideoSettings)
    consent: Consent = Field(default_factory=Consent)

    provider: FusionProvider = "omnihuman_v15"
    provider_options: Dict[str, Any] = Field(default_factory=dict)
    reference_image_urls: List[str] = Field(default_factory=list)
    reference_image_artifact_ids: List[str] = Field(default_factory=list)
    tags: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_inputs(self) -> "FusionJobCreate":
        if self.face_artifact_id is not None and not self.face_artifact_id.strip():
            self.face_artifact_id = None
        if self.heygen_talking_photo_id is not None and not self.heygen_talking_photo_id.strip():
            self.heygen_talking_photo_id = None
        if self.image_key is not None and not self.image_key.strip():
            self.image_key = None

        cleaned_artifact_ids: List[str] = []
        for artifact_id in self.reference_image_artifact_ids:
            if artifact_id is None:
                continue
            value = str(artifact_id).strip()
            if not value:
                continue
            try:
                uuid.UUID(value)
            except Exception:
                raise ValueError("reference_image_artifact_ids must contain valid UUIDs")
            cleaned_artifact_ids.append(value)
        self.reference_image_artifact_ids = cleaned_artifact_ids

        provider = _normalize_provider_name(self.provider)
        self.provider = provider  # normalize aliases for downstream orchestration

        has_face_url = self.face_image_url is not None
        has_face_artifact = bool(self.face_artifact_id)
        has_tp = bool(self.heygen_talking_photo_id)
        has_key = bool(self.image_key)
        has_refs = bool(self.reference_image_urls or self.reference_image_artifact_ids)
        provider_options = dict(self.provider_options or {})
        has_start_image = bool(provider_options.get("image_url") or provider_options.get("start_image_url"))

        if self.face_artifact_id:
            try:
                uuid.UUID(self.face_artifact_id)
            except Exception:
                raise ValueError("face_artifact_id must be a valid UUID")

        if provider == "heygen_av4":
            if not (has_face_url or has_face_artifact or has_tp or has_key):
                raise ValueError(
                    "heygen_av4 requires one of: face_image_url, face_artifact_id, heygen_talking_photo_id, image_key"
                )
        elif provider == "omnihuman_v15":
            if not (has_face_url or has_face_artifact):
                raise ValueError("omnihuman_v15 requires one of: face_image_url or face_artifact_id")
            if has_tp or has_key:
                raise ValueError("omnihuman_v15 does not use heygen_talking_photo_id or image_key")
        elif provider == "veed_fabric":
            if not (has_face_url or has_face_artifact or has_start_image):
                raise ValueError(
                    "veed_fabric requires one of: face_image_url, face_artifact_id, or provider_options.image_url/start_image_url"
                )
            duration_sec = getattr(self.video, "duration_sec", None)
            if duration_sec is not None and int(duration_sec) > 30:
                raise ValueError("veed_fabric currently supports duration_sec <= 30")
        else:
            if not (has_face_url or has_face_artifact or has_refs or has_start_image):
                raise ValueError(
                    "Provide one of: face_image_url, face_artifact_id, reference_image_urls, "
                    "reference_image_artifact_ids, or provider_options.image_url/start_image_url"
                )

        silent_provider = provider in {"kling", "luma", "runway"}

        if self.voice_mode == VoiceMode.audio:
            if self.voice_audio is None and not silent_provider:
                raise ValueError("voice_mode=audio requires voice_audio")
            if self.voice_tts is not None:
                raise ValueError("voice_mode=audio forbids voice_tts (set it to null).")
            return self

        if provider in {"omnihuman_v15", "veed_fabric"}:
            raise ValueError(f"{provider} currently supports voice_mode=audio only")

        if self.voice_mode == VoiceMode.tts:
            if self.voice_tts is None:
                raise ValueError("voice_mode=tts requires voice_tts")
            if self.voice_audio is not None:
                raise ValueError("voice_mode=tts forbids voice_audio (set it to null).")
            return self

        return self


class StepView(BaseModel):
    step_code: str
    status: str
    attempt: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class ArtifactView(BaseModel):
    kind: str
    url: str
    content_type: Optional[str] = None


class FusionJobView(BaseModel):
    job_id: str
    status: str
    provider: Optional[str] = None
    provider_job_id: Optional[str] = None

    steps: List[StepView] = Field(default_factory=list)
    artifacts: List[ArtifactView] = Field(default_factory=list)

    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pricing: Optional[Dict[str, Any]] = None
    pricing_summary: Optional[Dict[str, Any]] = None
