from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class HeyGenDimension(BaseModel):
    width: int = Field(ge=64, le=4096)
    height: int = Field(ge=64, le=4096)


class HeyGenTalkingPhotoCharacter(BaseModel):
    type: Literal["talking_photo"] = "talking_photo"
    talking_photo_id: str = Field(min_length=1)


class HeyGenAvatarCharacter(BaseModel):
    type: Literal["avatar"] = "avatar"
    avatar_id: str = Field(min_length=1)
    avatar_style: Optional[str] = "normal"


HeyGenCharacter = Union[HeyGenTalkingPhotoCharacter, HeyGenAvatarCharacter]


class HeyGenAudioVoice(BaseModel):
    type: Literal["audio"] = "audio"
    audio_url: Optional[str] = None
    audio_asset_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_rules(self):
        if not self.audio_url and not self.audio_asset_id:
            raise ValueError("voice.type=audio requires audio_url or audio_asset_id.")
        return self


class HeyGenTextVoice(BaseModel):
    type: Literal["text"] = "text"
    input_text: str = Field(min_length=1)
    voice_id: str = Field(min_length=1)


HeyGenVoice = Union[HeyGenAudioVoice, HeyGenTextVoice]


class HeyGenVideoInput(BaseModel):
    character: HeyGenCharacter
    voice: HeyGenVoice
    background: Optional[Dict[str, Any]] = None


class HeyGenV2GenerateRequest(BaseModel):
    video_inputs: List[HeyGenVideoInput] = Field(min_length=1)
    dimension: Optional[HeyGenDimension] = None
    aspect_ratio: Optional[str] = None

    # Kept top-level because this matched the successful manual request.
    use_avatar_iv_model: bool = True

    @model_validator(mode="after")
    def validate_rules(self):
        if not self.dimension and not self.aspect_ratio:
            raise ValueError("Either dimension or aspect_ratio must be provided.")
        return self


def validate_av4_payload(payload: Dict[str, Any]) -> None:
    # Kept function name for compatibility with the rest of Fusion.
    HeyGenV2GenerateRequest.model_validate(payload)