from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.domain.enums import AspectRatio, VoiceMode


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

class Consent(BaseModel):
    external_provider_ok: bool = False


class Dimension(BaseModel):
    width: int = Field(ge=64, le=4096)
    height: int = Field(ge=64, le=4096)


class VideoSettings(BaseModel):
    aspect_ratio: AspectRatio = AspectRatio.ar_9_16
    dimension: Optional[Dimension] = None

    duration_sec: Optional[int] = Field(default=None, ge=1, le=120)
    emotion: Optional[str] = Field(default=None, max_length=64)
    motion_style: Optional[str] = Field(default=None, max_length=64)


# -----------------------------------------------------------------------------
# Voice
# -----------------------------------------------------------------------------

class VoiceAudio(BaseModel):
    """
    Audio mode:
      - Prefer audio_url (Azure Blob SAS URL) OR audio_artifact_id (stable; svc-fusion can mint SAS).
      - audio_asset_id kept for backward compatibility.
    """
    type: Literal["audio"] = "audio"
    audio_url: Optional[HttpUrl] = None
    audio_asset_id: Optional[str] = None
    audio_artifact_id: Optional[str] = None  # stable reference into shared artifacts table

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


# -----------------------------------------------------------------------------
# Fusion Create + View
# -----------------------------------------------------------------------------

class FusionJobCreate(BaseModel):
    """
    UI -> svc-fusion contract:

    Face (one required):
      - face_image_url (SAS URL) OR face_artifact_id (preferred stable id)
      - heygen_talking_photo_id / image_key optional back-compat / advanced

    Voice:
      - voice_mode=audio: provide voice_audio (audio_url or audio_artifact_id)
      - voice_mode=tts: provide voice_tts
    """

    # Face inputs
    face_image_url: Optional[HttpUrl] = None
    face_artifact_id: Optional[str] = None
    heygen_talking_photo_id: Optional[str] = None
    image_key: Optional[str] = None

    # Voice inputs
    voice_mode: VoiceMode = VoiceMode.audio
    voice_audio: Optional[VoiceAudio] = None
    voice_tts: Optional[VoiceTTS] = None

    video: VideoSettings = Field(default_factory=VideoSettings)
    consent: Consent = Field(default_factory=Consent)

    provider: Literal["heygen_av4"] = "heygen_av4"
    tags: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_inputs(self) -> "FusionJobCreate":
        # Normalize optional strings
        if self.face_artifact_id is not None and not self.face_artifact_id.strip():
            self.face_artifact_id = None
        if self.heygen_talking_photo_id is not None and not self.heygen_talking_photo_id.strip():
            self.heygen_talking_photo_id = None
        if self.image_key is not None and not self.image_key.strip():
            self.image_key = None

        # Face input required
        has_face_url = self.face_image_url is not None
        has_face_artifact = bool(self.face_artifact_id)
        has_tp = bool(self.heygen_talking_photo_id)
        has_key = bool(self.image_key)

        if not (has_face_url or has_face_artifact or has_tp or has_key):
            raise ValueError("Provide one of: face_image_url, face_artifact_id, heygen_talking_photo_id, image_key")

        if self.face_artifact_id:
            try:
                uuid.UUID(self.face_artifact_id)
            except Exception:
                raise ValueError("face_artifact_id must be a valid UUID")

        # Voice rules
        if self.voice_mode == VoiceMode.audio:
            if not self.voice_audio:
                raise ValueError("voice_mode=audio requires voice_audio")
            # voice_tts is allowed as optional metadata in audio mode (do not forbid)
            return self

        # voice_mode == tts
        if not self.voice_tts:
            raise ValueError("voice_mode=tts requires voice_tts")
        if self.voice_audio is not None:
            raise ValueError("voice_mode=tts forbids voice_audio (set it to null).")
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
    provider_job_id: Optional[str] = None

    steps: List[StepView] = Field(default_factory=list)
    artifacts: List[ArtifactView] = Field(default_factory=list)

    error_code: Optional[str] = None
    error_message: Optional[str] = None