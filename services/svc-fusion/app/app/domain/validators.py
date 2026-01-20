from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from app.domain.enums import VoiceMode
from app.domain.models import FusionJobCreate


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except Exception:
        return False


def validate_fusion_request(req: FusionJobCreate) -> None:
    if not req.consent.external_provider_ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Consent required: external_provider_ok must be true for HeyGen.",
        )

    # Face source must exist (model validator already enforces; keep safety here)
    if not (req.face_image_url or req.face_artifact_id or req.heygen_talking_photo_id or req.image_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide one of: face_image_url, face_artifact_id, heygen_talking_photo_id, image_key",
        )

    if req.face_artifact_id and not _is_uuid(req.face_artifact_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="face_artifact_id must be a valid UUID",
        )

    if req.voice_mode == VoiceMode.audio:
        if req.voice_audio is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="voice_mode=audio requires voice_audio",
            )

        # voice_audio must have one of: audio_url | audio_asset_id | audio_artifact_id
        # (model validator enforces this too, but we keep a guard here)
        audio_artifact_id = getattr(req.voice_audio, "audio_artifact_id", None)
        if audio_artifact_id and not _is_uuid(audio_artifact_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="voice_audio.audio_artifact_id must be a valid UUID",
            )

        # DO NOT forbid voice_tts in audio mode (optional metadata)
        return

    # voice_mode == tts
    if req.voice_mode == VoiceMode.tts:
        if req.voice_tts is None:
            raise HTTPException(status_code=400, detail="voice_tts is required for voice_mode=tts")
        if req.voice_audio is not None:
            raise HTTPException(status_code=400, detail="voice_audio must be null for voice_mode=tts")
        return