from __future__ import annotations

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, model_validator


class HeyGenDimension(BaseModel):
    width: int = Field(ge=64, le=4096)
    height: int = Field(ge=64, le=4096)


class HeyGenAV4Request(BaseModel):
    """
    AV4 supports two mutually exclusive voice modes:

    - Audio mode: talking_photo_id + audio_url
    - TTS mode:   talking_photo_id + voice_id + script

    Also requires either dimension or aspect_ratio.
    """
    test: bool = False

    image_key: str = Field(min_length=1)
    video_title: str = Field(min_length=1)

    dimension: Optional[HeyGenDimension] = None
    aspect_ratio: Optional[str] = None

    # Audio mode
    audio_url: Optional[str] = None

    # Back-compat field (we accept it in payload struct, but DO NOT recommend using it)
    # If you truly want to support it, convert asset_id -> url outside, then pass audio_url.
    audio_asset_id: Optional[str] = None

    # TTS mode
    voice_id: Optional[str] = None
    script: Optional[str] = None

    @model_validator(mode="after")
    def validate_rules(self):
        # size rule
        if not self.dimension and not self.aspect_ratio:
            raise ValueError("Either dimension or aspect_ratio must be provided.")

        has_audio = bool(self.audio_url) or bool(self.audio_asset_id)
        has_tts = bool(self.voice_id) or bool(self.script)

        # Must choose exactly one mode
        if has_audio and has_tts:
            raise ValueError("Provide either (audio_url/audio_asset_id) OR (voice_id+script), not both.")
        if not has_audio and not has_tts:
            raise ValueError("Provide either audio_url (audio mode) OR voice_id+script (tts mode).")

        # Audio mode rules
        if has_audio:
            # strongly prefer audio_url; if asset_id provided alone, we allow validation but orchestration should resolve it
            if not self.audio_url and self.audio_asset_id:
                # allow pass-through for now, but warn in logs at call site if you want
                return self
            if not self.audio_url:
                raise ValueError("Audio mode requires audio_url.")
            return self

        # TTS mode rules
        if not self.voice_id:
            raise ValueError("TTS mode requires voice_id.")
        if not self.script:
            raise ValueError("TTS mode requires script.")
        return self


def validate_av4_payload(payload: Dict[str, Any]) -> None:
    HeyGenAV4Request.model_validate(payload)