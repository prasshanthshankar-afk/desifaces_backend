from __future__ import annotations

import asyncio
import os
from typing import Any

from .fusion_execution import FusionSceneContext, _clean, _scene_prompt


def _input_concurrency() -> int:
    raw = str(os.getenv("DF_DIRECTOR_FUSION_INPUT_CONCURRENCY", "32") or "32").strip()
    try:
        return max(1, min(64, int(raw)))
    except Exception:
        return 32


async def compile_children_performant(
    *,
    context: FusionSceneContext,
    face_client,
    audio_client,
    headers: dict[str, str],
    external_provider_ok: bool,
    request_nonce_by_turn: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Compile canonical child requests with bounded parallel media resolution.

    The canonical payload is unchanged: one approved Face + one approved Audio asset
    per speech turn, canonical scene prompt, VEED/Fabric child execution and the same
    Director lineage tags. Repeated Face media is resolved once per compilation and
    all unique Face/Audio read URLs are resolved concurrently up to the deployment
    limit. V3 defaults to 32 so a 28-turn scene does not acquire an artificial
    preparation queue before provider dispatch.
    """
    semaphore = asyncio.Semaphore(_input_concurrency())
    face_urls: dict[str, str] = {}
    audio_urls: dict[str, str] = {}

    async def load_face(media_id) -> None:
        key = str(media_id)
        async with semaphore:
            face_urls[key] = await face_client.read_url(headers=headers, media_id=media_id)

    async def load_audio(media_id) -> None:
        key = str(media_id)
        async with semaphore:
            audio_urls[key] = await audio_client.read_url(headers=headers, media_id=media_id)

    unique_faces = {str(turn.face_media_id): turn.face_media_id for turn in context.turns}
    unique_audio = {str(turn.audio_media_id): turn.audio_media_id for turn in context.turns}
    await asyncio.gather(
        *(load_face(media_id) for media_id in unique_faces.values()),
        *(load_audio(media_id) for media_id in unique_audio.values()),
    )

    prompt = _scene_prompt(context)
    children: list[dict[str, Any]] = []
    for turn in context.turns:
        face_url = face_urls[str(turn.face_media_id)]
        audio_url = audio_urls[str(turn.audio_media_id)]
        video: dict[str, Any] = {}
        if turn.duration_hint_ms and turn.duration_hint_ms > 0:
            video["duration_sec"] = max(1, min(30, int(round(turn.duration_hint_ms / 1000.0))))
        if turn.emotion_code:
            video["emotion"] = turn.emotion_code
        if prompt:
            video["prompt"] = prompt

        turn_key = str(turn.dialogue_turn_id)
        request_nonce = _clean((request_nonce_by_turn or {}).get(turn_key))
        payload: dict[str, Any] = {
            "face_image_url": face_url,
            "provider": "veed_fabric",
            "voice_mode": "audio",
            "voice_audio": {"type": "audio", "audio_url": audio_url},
            "consent": {"external_provider_ok": bool(external_provider_ok)},
            "video": video,
            "tags": {
                "v3_orchestrated": True,
                "workflow_id": str(context.workflow_id),
                "scene_id": str(context.scene_id),
                "stage_run_id": str(context.stage_run_id),
                "dialogue_turn_id": turn_key,
                "participant_id": str(turn.participant_id),
                "segment_sequence": turn.sequence_no,
            },
        }
        if request_nonce:
            payload["provider_options"] = {"v3_request_nonce": request_nonce}

        children.append({
            "dialogue_turn_id": turn_key,
            "participant_id": str(turn.participant_id),
            "display_name": turn.display_name,
            "sequence_no": turn.sequence_no,
            "face_media_id": str(turn.face_media_id),
            "audio_media_id": str(turn.audio_media_id),
            "payload": payload,
        })

    return children


__all__ = ["compile_children_performant", "_input_concurrency"]
