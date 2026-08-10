from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional

from app.domain.enums import VoiceMode
from app.domain.models import FusionJobCreate
from app.services.providers.heygen.mapper import resolve_dimension
from app.services.providers.heygen.av4_contract import validate_av4_payload


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _sanitize_background_override(background_override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Keep background override provider-safe and explicitly non-script-like.
    """
    default_bg: Dict[str, Any] = {"type": "color", "value": "#FFFFFF"}

    if not isinstance(background_override, dict) or not background_override:
        return default_bg

    bg = deepcopy(background_override)

    # Strip any fields that could accidentally drift into script/prompt territory.
    for key in (
        "script",
        "input_text",
        "text",
        "caption",
        "subtitle",
        "subtitle_text",
        "voiceover_text",
        "spoken_text",
        "prompt",
        "creative_prompt",
        "word_timing",
        "word_timings",
        "word_time_metadata",
        "timed_metadata",
    ):
        bg.pop(key, None)

    bg_type = _clean_str(bg.get("type"))
    if not bg_type:
        return default_bg

    bg["type"] = bg_type

    # Normalize simplest supported shapes and fall back safely.
    if bg_type == "color":
        bg["value"] = _clean_str(bg.get("value")) or "#FFFFFF"
        return {"type": "color", "value": bg["value"]}

    # For non-color background types, keep only non-empty scalar/list/dict values.
    cleaned: Dict[str, Any] = {"type": bg_type}
    for k, v in bg.items():
        if k == "type":
            continue
        if v is None:
            continue
        if isinstance(v, str):
            s = v.strip()
            if s:
                cleaned[k] = s
        else:
            cleaned[k] = v

    return cleaned if len(cleaned) > 1 else default_bg


def build_av4_payload(
    req: FusionJobCreate,
    *,
    talking_photo_id: str,
    video_title: str,
    audio_url_override: Optional[str] = None,
    background_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build the HeyGen V2 create-video payload for DesiFaces exact-audio Fusion.

    Hard rules:
    - voice_mode=audio emits a pure audio-backed payload and never includes text/script fields.
    - voice_mode=tts emits a pure text-backed payload.
    - background_override is sanitized so it cannot inject prompt/script-like fields.
    - video_title is retained for caller compatibility but is intentionally unused in payload.
    """
    talking_photo_id_clean = _clean_str(talking_photo_id)
    if not talking_photo_id_clean:
        raise ValueError("talking_photo_id is required")

    dim = resolve_dimension(req.video)

    character: Dict[str, Any] = {
        "type": "talking_photo",
        "talking_photo_id": talking_photo_id_clean,
    }

    if req.voice_mode == VoiceMode.audio:
        audio_url = _clean_str(audio_url_override)
        if not audio_url:
            if not req.voice_audio:
                raise ValueError("voice_mode=audio requires voice_audio")
            audio_url = _clean_str(req.voice_audio.audio_url)

        if not audio_url:
            raise ValueError("voice_mode=audio requires voice_audio.audio_url (or audio_url_override)")

        # Pure exact-audio payload: do not leak any text/script fields.
        voice: Dict[str, Any] = {
            "type": "audio",
            "audio_url": audio_url,
        }

    else:
        voice_id = _clean_str(req.voice_tts.voice_id) if req.voice_tts else None
        script = _clean_str(req.voice_tts.script) if req.voice_tts else None
        if not voice_id or not script:
            raise ValueError("voice_mode=tts requires voice_tts.voice_id and voice_tts.script")

        voice = {
            "type": "text",
            "input_text": script,
            "voice_id": voice_id,
        }

    payload: Dict[str, Any] = {
        "video_inputs": [
            {
                "character": character,
                "voice": voice,
                "background": _sanitize_background_override(background_override),
            }
        ],
        "use_avatar_iv_model": True,
    }

    if dim:
        payload["dimension"] = {"width": dim.width, "height": dim.height}
    else:
        payload["aspect_ratio"] = req.video.aspect_ratio.value

    validate_av4_payload(payload)
    return payload
