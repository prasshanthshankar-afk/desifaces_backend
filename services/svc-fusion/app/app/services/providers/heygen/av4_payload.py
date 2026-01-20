from __future__ import annotations

from typing import Any, Dict, Optional

from app.domain.enums import VoiceMode
from app.domain.models import FusionJobCreate
from app.services.providers.heygen.mapper import resolve_dimension
from app.services.providers.heygen.av4_contract import validate_av4_payload


def build_av4_payload(
    req: FusionJobCreate,
    *,
    talking_photo_id: str,
    video_title: str,
    audio_url_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build HeyGen AV4 payload.

    We support two modes:

    1) voice_mode=audio (recommended)
       - Requires: talking_photo_id + audio_url
       - Does NOT require voice_id/script

    2) voice_mode=tts
       - Requires: talking_photo_id + voice_id + script
       - Does NOT require audio_url
    """
    if not talking_photo_id or not talking_photo_id.strip():
        raise ValueError("talking_photo_id is required")

    dim = resolve_dimension(req.video)

    payload: Dict[str, Any] = {
        "test": False,
        "video_title": video_title,
        "image_key": talking_photo_id,
    }

    # -------------------------
    # Voice selection
    # -------------------------
    if req.voice_mode == VoiceMode.audio:
        # Prefer override (caller can pass Azure SAS directly or other URL)
        audio_url = audio_url_override
        if not audio_url:
            if not req.voice_audio:
                raise ValueError("voice_mode=audio requires voice_audio")
            if not req.voice_audio.audio_url:
                raise ValueError("voice_mode=audio requires voice_audio.audio_url (or audio_url_override)")
            audio_url = str(req.voice_audio.audio_url)

        payload["audio_url"] = str(audio_url)

    else:
        # voice_mode == tts
        if not req.voice_tts or not req.voice_tts.voice_id or not req.voice_tts.script:
            raise ValueError("voice_mode=tts requires voice_tts.voice_id and voice_tts.script")
        payload["voice_id"] = req.voice_tts.voice_id
        payload["script"] = req.voice_tts.script

    # -------------------------
    # Sizing
    # -------------------------
    if dim:
        payload["dimension"] = {"width": dim.width, "height": dim.height}
    else:
        payload["aspect_ratio"] = req.video.aspect_ratio.value

    validate_av4_payload(payload)
    return payload