from __future__ import annotations

import asyncio
import os
from typing import Any

from .fusion_execution import FusionSceneContext, _clean, _scene_prompt


_ALLOWED_ASPECT_RATIOS = frozenset({"9:16", "16:9", "1:1"})


def _input_concurrency() -> int:
    raw = str(os.getenv("DF_DIRECTOR_FUSION_INPUT_CONCURRENCY", "32") or "32").strip()
    try:
        return max(1, min(64, int(raw)))
    except Exception:
        return 32


def _scene_aspect_ratio(context: FusionSceneContext) -> str:
    raw = _clean((context.stage_metadata or {}).get("aspect_ratio") or "9:16")
    return raw if raw in _ALLOWED_ASPECT_RATIOS else "9:16"


def _assert_conversation_mode_supported(context: FusionSceneContext) -> dict[str, Any] | None:
    """Validate and return the optional shared-scene provider contract."""

    mode = _clean((context.stage_metadata or {}).get("conversation_mode")).casefold()
    if not mode:
        return None
    if mode != "shared_scene":
        raise RuntimeError(f"unsupported_fusion_conversation_mode:{mode}")

    metadata = context.stage_metadata or {}
    shared_scene_media_id = _clean(metadata.get("shared_scene_media_id"))
    speaker_targets = metadata.get("speaker_targets")
    dimensions = metadata.get("shared_scene_dimensions")
    if not shared_scene_media_id:
        raise RuntimeError("shared_scene_media_id_required")
    if not isinstance(speaker_targets, dict):
        raise RuntimeError("shared_scene_speaker_targets_required")
    if not isinstance(dimensions, dict):
        raise RuntimeError("shared_scene_dimensions_required")

    try:
        image_width = int(dimensions.get("width"))
        image_height = int(dimensions.get("height"))
    except Exception as exc:
        raise RuntimeError("shared_scene_dimensions_invalid") from exc
    if image_width < 64 or image_height < 64:
        raise RuntimeError("shared_scene_dimensions_invalid")

    missing = [
        str(turn.participant_id)
        for turn in context.turns
        if not isinstance(speaker_targets.get(str(turn.participant_id)), dict)
    ]
    if missing:
        raise RuntimeError("shared_scene_speaker_targets_missing:" + ",".join(sorted(set(missing))))

    return {
        "shared_scene_media_id": shared_scene_media_id,
        "image_width": image_width,
        "image_height": image_height,
        "speaker_targets": speaker_targets,
    }


def _shared_scene_video_provider(context: FusionSceneContext) -> str:
    provider = _clean((context.stage_metadata or {}).get("shared_scene_video_provider")).casefold()
    return provider if provider in {"sync3", "omnihuman_v15"} else "sync3"


def _shared_scene_video_prompt(context: FusionSceneContext) -> str:
    return _clean((context.stage_metadata or {}).get("shared_scene_video_prompt"))


def _all_speaker_coordinates(shared_scene: dict[str, Any]) -> list[list[int]]:
    coords: list[list[int]] = []
    for participant_id in shared_scene["speaker_targets"].keys():
        try:
            coords.append(_speaker_coordinates(shared_scene, participant_id))
        except Exception:
            continue
    return coords


def _speaker_coordinates(shared_scene: dict[str, Any], participant_id) -> list[int]:
    target = shared_scene["speaker_targets"].get(str(participant_id))
    if not isinstance(target, dict):
        raise RuntimeError(f"shared_scene_speaker_target_missing:{participant_id}")

    image_width = int(shared_scene["image_width"])
    image_height = int(shared_scene["image_height"])

    point = target.get("point")
    if isinstance(point, dict):
        try:
            center_x_norm = float(point.get("x"))
            center_y_norm = float(point.get("y"))
        except Exception as exc:
            raise RuntimeError(f"shared_scene_speaker_target_invalid:{participant_id}") from exc
        if not (0.0 <= center_x_norm <= 1.0 and 0.0 <= center_y_norm <= 1.0):
            raise RuntimeError(f"shared_scene_speaker_target_invalid:{participant_id}")
    else:
        box = target.get("box")
        if not isinstance(box, dict):
            raise RuntimeError(f"shared_scene_speaker_target_invalid:{participant_id}")
        try:
            x = float(box.get("x"))
            y = float(box.get("y"))
            width = float(box.get("width"))
            height = float(box.get("height"))
        except Exception as exc:
            raise RuntimeError(f"shared_scene_speaker_target_invalid:{participant_id}") from exc
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            raise RuntimeError(f"shared_scene_speaker_target_invalid:{participant_id}")
        center_x_norm = x + width / 2.0
        center_y_norm = y + height / 2.0

    center_x = max(0, min(image_width - 1, int(round(center_x_norm * image_width))))
    center_y = max(0, min(image_height - 1, int(round(center_y_norm * image_height))))
    return [center_x, center_y]


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

    The selected scene aspect ratio is persisted on the Fusion stage before
    pricing. Both pricing and dispatch reload the same stage metadata, ensuring
    that 9:16, 16:9 or 1:1 cannot drift between quote and provider execution.
    """
    shared_scene = _assert_conversation_mode_supported(context)

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

    if shared_scene:
        shared_media_id = shared_scene["shared_scene_media_id"]
        await asyncio.gather(
            load_face(shared_media_id),
            *(load_audio(media_id) for media_id in {str(turn.audio_media_id): turn.audio_media_id for turn in context.turns}.values()),
        )
    else:
        unique_faces = {str(turn.face_media_id): turn.face_media_id for turn in context.turns}
        unique_audio = {str(turn.audio_media_id): turn.audio_media_id for turn in context.turns}
        await asyncio.gather(
            *(load_face(media_id) for media_id in unique_faces.values()),
            *(load_audio(media_id) for media_id in unique_audio.values()),
        )

    prompt = _scene_prompt(context)
    aspect_ratio = _scene_aspect_ratio(context)
    children: list[dict[str, Any]] = []
    for turn in context.turns:
        face_url = face_urls[str(shared_scene["shared_scene_media_id"])] if shared_scene else face_urls[str(turn.face_media_id)]
        audio_url = audio_urls[str(turn.audio_media_id)]
        video: dict[str, Any] = {"aspect_ratio": aspect_ratio}
        if turn.duration_hint_ms and turn.duration_hint_ms > 0:
            video["duration_sec"] = max(1, min(30, int(round(turn.duration_hint_ms / 1000.0))))
        if turn.emotion_code:
            video["emotion"] = turn.emotion_code
        if prompt:
            video["prompt"] = prompt

        turn_key = str(turn.dialogue_turn_id)
        request_nonce = _clean((request_nonce_by_turn or {}).get(turn_key))
        provider_options: dict[str, Any] = {}
        provider_name = "veed_fabric"
        if shared_scene:
            provider_name = _shared_scene_video_provider(context)
            active_coords = _speaker_coordinates(shared_scene, turn.participant_id)
            provider_options["active_speaker_coordinates"] = active_coords
            provider_options["conversation_mode"] = "shared_scene"
            provider_options["shared_scene_media_id"] = shared_scene["shared_scene_media_id"]
            provider_options["shared_scene_dimensions"] = {
                "width": shared_scene["image_width"],
                "height": shared_scene["image_height"],
            }
            provider_options["all_speaker_coordinates"] = _all_speaker_coordinates(shared_scene)
            if provider_name == "omnihuman_v15":
                prompt_text = _shared_scene_video_prompt(context)
                if prompt_text:
                    provider_options["prompt"] = prompt_text
                provider_options["resolution"] = "720p"
                provider_options["turbo_mode"] = False

        payload: dict[str, Any] = {
            "face_image_url": face_url,
            "provider": provider_name,
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
                "aspect_ratio": aspect_ratio,
                "conversation_mode": "shared_scene" if shared_scene else "ordered_speaker_shots",
                "shared_scene_video_provider": provider_name if shared_scene else None,
                "shared_scene_motion_mode": _clean((context.stage_metadata or {}).get("shared_scene_motion_mode")) if shared_scene else None,
            },
        }
        if request_nonce:
            provider_options["v3_request_nonce"] = request_nonce
        if provider_options:
            payload["provider_options"] = provider_options

        children.append({
            "dialogue_turn_id": turn_key,
            "participant_id": str(turn.participant_id),
            "display_name": turn.display_name,
            "sequence_no": turn.sequence_no,
            "face_media_id": str(turn.face_media_id) if turn.face_media_id is not None else None,
            "shared_scene_media_id": shared_scene["shared_scene_media_id"] if shared_scene else None,
            "audio_media_id": str(turn.audio_media_id),
            "aspect_ratio": aspect_ratio,
            "payload": payload,
        })

    return children


__all__ = [
    "compile_children_performant",
    "_input_concurrency",
    "_scene_aspect_ratio",
    "_assert_conversation_mode_supported",
    "_speaker_coordinates",
    "_shared_scene_video_provider",
    "_shared_scene_video_prompt",
]
