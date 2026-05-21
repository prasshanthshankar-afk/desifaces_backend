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


def _normalize_provider_name(value: str | None) -> str:
    provider = str(value or "omnihuman_v15").strip().lower()
    if provider in {"veed", "veed_fabric", "fabric", "veed/fabric-1.0"}:
        return "veed_fabric"
    if provider in {"omnihuman", "omnihuman_v15"}:
        return "omnihuman_v15"
    return provider or "omnihuman_v15"


def validate_fusion_request(req: FusionJobCreate) -> None:
    provider = _normalize_provider_name(getattr(req, "provider", None))

    if not req.consent.external_provider_ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Consent required: external_provider_ok must be true for external providers.",
        )

    has_face = bool(req.face_image_url or req.face_artifact_id or req.heygen_talking_photo_id or req.image_key)
    has_refs = bool(getattr(req, "reference_image_urls", None) or getattr(req, "reference_image_artifact_ids", None))
    provider_options = dict(getattr(req, "provider_options", None) or {})
    has_start_image = bool(provider_options.get("image_url") or provider_options.get("start_image_url"))

    if provider == "heygen_av4":
        if not has_face:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide one of: face_image_url, face_artifact_id, heygen_talking_photo_id, image_key",
            )

    elif provider == "omnihuman_v15":
        if not bool(req.face_image_url or req.face_artifact_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="omnihuman_v15 requires one of: face_image_url or face_artifact_id",
            )
        if bool(req.heygen_talking_photo_id or req.image_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="omnihuman_v15 does not use heygen_talking_photo_id or image_key",
            )

    elif provider == "veed_fabric":
        if not bool(req.face_image_url or req.face_artifact_id or has_start_image):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="veed_fabric requires one of: face_image_url, face_artifact_id, or provider_options.image_url/start_image_url",
            )
        duration_sec = getattr(getattr(req, "video", None), "duration_sec", None)
        if duration_sec is not None and int(duration_sec) > 30:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="veed_fabric currently supports duration_sec <= 30",
            )

    else:
        if not (has_face or has_refs or has_start_image):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Provide one of: face_image_url, face_artifact_id, reference_image_urls, "
                    "reference_image_artifact_ids, or provider_options.image_url/start_image_url"
                ),
            )

    if req.face_artifact_id and not _is_uuid(req.face_artifact_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="face_artifact_id must be a valid UUID",
        )

    if req.voice_mode == VoiceMode.audio:
        if req.voice_audio is None and provider not in {"kling", "luma", "runway"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="voice_mode=audio requires voice_audio",
            )

        if req.voice_audio is not None:
            audio_artifact_id = getattr(req.voice_audio, "audio_artifact_id", None)
            if audio_artifact_id and not _is_uuid(audio_artifact_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="voice_audio.audio_artifact_id must be a valid UUID",
                )
        return

    if provider in {"omnihuman_v15", "veed_fabric"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{provider} currently supports voice_mode=audio only",
        )

    if req.voice_mode == VoiceMode.tts:
        if req.voice_tts is None:
            raise HTTPException(status_code=400, detail="voice_tts is required for voice_mode=tts")
        if req.voice_audio is not None:
            raise HTTPException(status_code=400, detail="voice_audio must be null for voice_mode=tts")
        return
