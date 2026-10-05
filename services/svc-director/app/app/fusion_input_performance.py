from __future__ import annotations

import asyncio
import os
from typing import Any

from .fusion_execution import FusionSceneContext, _clean, _scene_prompt


_ALLOWED_ASPECT_RATIOS = frozenset({"9:16", "16:9", "1:1"})
_SHARED_SCENE_VIDEO_STYLES = frozenset({"static_video", "cinematic_video"})
_SHARED_SCENE_CAMERA_MODES = frozenset(
    {
        "director_choice",
        "static",
        "push_in",
        "push_out",
        "arc_left",
        "arc_right",
        "angle_shift_low_to_eye",
        "angle_shift_high_to_eye",
    }
)

_CAMERA_DIRECTIONS = {
    "static": (
        "A static eye-level medium two-shot holds both people naturally in frame throughout. "
        "There is no camera movement."
    ),
    "push_in": (
        "The camera begins with an eye-level medium two-shot and performs a very slow, smooth push-in, "
        "ending in a slightly tighter two-shot while keeping both people visible."
    ),
    "push_out": (
        "The camera begins in a slightly tighter eye-level two-shot and slowly, smoothly pulls back to a "
        "comfortable medium two-shot while keeping both people visible."
    ),
    "arc_left": (
        "The camera makes a very gentle, slow arc a few degrees to the left, creating subtle natural parallax "
        "while both faces remain continuously visible."
    ),
    "arc_right": (
        "The camera makes a very gentle, slow arc a few degrees to the right, creating subtle natural parallax "
        "while both faces remain continuously visible."
    ),
    "angle_shift_low_to_eye": (
        "The camera begins from a subtly lower conversational angle and smoothly settles to eye level, "
        "keeping both people visible without a dramatic perspective change."
    ),
    "angle_shift_high_to_eye": (
        "The camera begins from a subtly higher conversational angle and smoothly settles to eye level, "
        "keeping both people visible without a dramatic perspective change."
    ),
}


def _input_concurrency() -> int:
    raw = str(os.getenv("DF_DIRECTOR_FUSION_INPUT_CONCURRENCY", "32") or "32").strip()
    try:
        return max(1, min(64, int(raw)))
    except Exception:
        return 32


def _scene_aspect_ratio(context: FusionSceneContext) -> str:
    raw = _clean((context.stage_metadata or {}).get("aspect_ratio") or "9:16")
    return raw if raw in _ALLOWED_ASPECT_RATIOS else "9:16"


def _normalize_video_style(value: Any) -> str:
    raw = _clean(value).casefold()
    if raw in {"", "natural_motion", "precise_lipsync", "static", "normal"}:
        return "static_video"
    if raw in {"cinematic", "cinematic_motion", "cinematic_video"}:
        return "cinematic_video"
    if raw not in _SHARED_SCENE_VIDEO_STYLES:
        raise RuntimeError(f"unsupported_shared_scene_video_style:{raw}")
    return raw


def _normalize_camera_mode(value: Any, *, video_style: str) -> str:
    raw = _clean(value).casefold()
    if not raw:
        return "director_choice" if video_style == "cinematic_video" else "static"
    aliases = {
        "pushin": "push_in",
        "dolly_in": "push_in",
        "dolly-in": "push_in",
        "pushout": "push_out",
        "pull_out": "push_out",
        "pull-back": "push_out",
        "orbit_left": "arc_left",
        "orbit_right": "arc_right",
        "arc": "arc_right",
        "low_to_eye": "angle_shift_low_to_eye",
        "high_to_eye": "angle_shift_high_to_eye",
    }
    raw = aliases.get(raw, raw)
    if video_style == "static_video":
        return "static"
    if raw not in _SHARED_SCENE_CAMERA_MODES:
        raise RuntimeError(f"unsupported_shared_scene_camera_mode:{raw}")
    return raw


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

    video_style = _normalize_video_style(
        metadata.get("shared_scene_video_style")
        or metadata.get("shared_scene_motion_mode")
        or "static_video"
    )
    camera_mode = _normalize_camera_mode(
        metadata.get("shared_scene_camera_mode"),
        video_style=video_style,
    )

    return {
        "shared_scene_media_id": shared_scene_media_id,
        "image_width": image_width,
        "image_height": image_height,
        "speaker_targets": speaker_targets,
        "video_style": video_style,
        "camera_mode": camera_mode,
        "video_prompt": _clean(metadata.get("shared_scene_video_prompt")) or None,
        "resolution": _clean(metadata.get("shared_scene_resolution")) or "720p",
    }


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


def _speaker_position_label(shared_scene: dict[str, Any], participant_id) -> str:
    x, _ = _speaker_coordinates(shared_scene, participant_id)
    width = max(1, int(shared_scene["image_width"]))
    ratio = float(x) / float(width)
    if ratio <= 0.40:
        return "on the left side of the image"
    if ratio >= 0.60:
        return "on the right side of the image"
    return "near the center of the image"


def _scene_camera_hint(context: FusionSceneContext) -> str:
    return _clean((context.scene_direction or {}).get("camera")).casefold()


def _ambient_motion_plan(context: FusionSceneContext) -> str:
    """Return one scene-level ambient-motion plan reused for every dialogue turn.

    Static Video locks the camera, not the world. Background movement stays
    subtle and continuity-safe so independently rendered speaker turns still
    feel like one living scene.
    """

    setting = context.scene_setting or {}
    direction = context.scene_direction or {}
    visual = direction.get("visual") if isinstance(direction.get("visual"), dict) else {}
    performance = direction.get("performance") if isinstance(direction.get("performance"), dict) else {}

    scene_text = " ".join(
        _clean(value)
        for value in (
            context.scene_title,
            context.scene_summary,
            *setting.values(),
            *visual.values(),
            *performance.values(),
        )
        if _clean(value)
    ).casefold()

    base = (
        "Ambient scene motion plan: keep the original background layout, landmarks, furniture, lighting direction, "
        "depth relationships, and perspective stable, but do not freeze the world. Use only subtle, believable "
        "background movement that remains secondary to the conversation. Background people must never speak, "
        "approach the foreground, cross in front of either main speaker, or become a new focal subject."
    )

    outdoor_tokens = (
        "outdoor", "mountain", "park", "street", "market", "festival", "beach",
        "terrace", "plaza", "overlook", "garden", "city", "village",
    )
    indoor_public_tokens = (
        "cafe", "restaurant", "workspace", "office", "lobby", "store", "shop",
        "studio", "station", "airport", "hall",
    )

    if any(token in scene_text for token in outdoor_tokens):
        detail = (
            "Allow small distant non-speaking passersby or already-present background people to walk slowly and "
            "naturally when consistent with the source scene; keep their faces indistinct. Add gentle motion to "
            "clouds, foliage, loose fabric, flags, distant traffic, water, or similar naturally movable elements "
            "when present."
        )
    elif any(token in scene_text for token in indoor_public_tokens):
        detail = (
            "Allow small distant non-speaking background people to make restrained natural movements when consistent "
            "with the source scene, such as walking behind the conversation or shifting at a table; keep them soft "
            "and non-prominent. Screens, steam, window traffic, curtains, plants, or practical lights may move subtly "
            "when present."
        )
    else:
        detail = (
            "Animate only naturally movable scene elements with restrained motion. If distant background people are "
            "already present or contextually appropriate, they may make small non-speaking movements while remaining "
            "indistinct and visually secondary."
        )

    return f"{base} {detail}"[:1600]


def _director_camera_mode(
    context: FusionSceneContext,
    shared_scene: dict[str, Any],
    turn,
) -> tuple[str, str]:
    """Choose one coherent turn camera treatment for cinematic group conversation.

    Static Video never moves the camera. Cinematic Video first honors explicit
    Creative Director scene camera language and otherwise derives a restrained
    dialogue-safe camera treatment from turn emotion and position in the scene.
    """

    video_style = str(shared_scene.get("video_style") or "static_video")
    requested = str(shared_scene.get("camera_mode") or "static")
    if video_style == "static_video":
        return "static", "static_video"

    if requested != "director_choice":
        return requested, "explicit_cinematic_setting"

    hint = _scene_camera_hint(context)
    hint_map = (
        (("push in", "push-in", "dolly in", "dolly-in", "closer"), "push_in"),
        (("push out", "push-out", "pull back", "pull-back", "wider"), "push_out"),
        (("arc left", "orbit left"), "arc_left"),
        (("arc right", "orbit right", "orbit", "arc"), "arc_right"),
        (("low angle", "low-angle"), "angle_shift_low_to_eye"),
        (("high angle", "high-angle"), "angle_shift_high_to_eye"),
        (("static", "locked", "tripod"), "static"),
    )
    for markers, mode in hint_map:
        if any(marker in hint for marker in markers):
            return mode, "creative_director_scene_direction"

    emotion = _clean(getattr(turn, "emotion_code", None)).casefold()
    total_turns = max(1, len(context.turns))
    sequence = max(1, int(getattr(turn, "sequence_no", 1) or 1))

    if sequence >= total_turns and total_turns > 2:
        return "push_out", "creative_director_contextual_policy_v1"

    if any(token in emotion for token in ("playful", "excited", "energetic", "celebrat", "animated")):
        active_x, _ = _speaker_coordinates(shared_scene, turn.participant_id)
        return (
            "arc_right" if active_x <= int(shared_scene["image_width"]) // 2 else "arc_left",
            "creative_director_contextual_policy_v1",
        )

    if any(token in emotion for token in ("serious", "assertive", "confident", "dramatic")):
        return "angle_shift_low_to_eye", "creative_director_contextual_policy_v1"

    if any(token in emotion for token in ("reflect", "warm", "sincere", "tender", "reassur", "thoughtful", "sad")):
        return "push_in", "creative_director_contextual_policy_v1"

    # Dialogue-safe coverage when the Director has no explicit camera cue.
    cycle = ("push_in", "arc_right", "static", "arc_left")
    return cycle[(sequence - 1) % len(cycle)], "creative_director_contextual_policy_v1"


def _shared_scene_performance_prompt(
    context: FusionSceneContext,
    shared_scene: dict[str, Any],
    turn,
    *,
    scene_prompt: str | None,
    camera_mode: str,
) -> str:
    """Build turn-specific OmniHuman acting and camera direction."""

    metadata = context.stage_metadata or {}
    saved_direction = _clean(metadata.get("shared_scene_video_prompt"))
    active_name = _clean(turn.display_name) or "the active speaker"
    active_position = _speaker_position_label(shared_scene, turn.participant_id)

    listener = next(
        (
            candidate
            for candidate in context.turns
            if candidate.participant_id != turn.participant_id
        ),
        None,
    )
    listener_name = _clean(getattr(listener, "display_name", None)) or "the other person"
    listener_position = (
        _speaker_position_label(shared_scene, listener.participant_id)
        if listener is not None
        else "elsewhere in the same group photo"
    )

    emotion = _clean(turn.emotion_code)
    dialogue = _clean(getattr(turn, "dialogue_text", None))
    camera_direction = _CAMERA_DIRECTIONS.get(camera_mode, _CAMERA_DIRECTIONS["static"])
    ambient_motion = _ambient_motion_plan(context)

    parts = [
        camera_direction,
        ambient_motion,
        saved_direction,
        scene_prompt,
        (
            f"Natural two-person conversation. {active_name}, {active_position}, is the only person speaking "
            f"during this turn. {listener_name}, {listener_position}, is listening."
        ),
        (
            f"Lip-sync is strict for {active_name}: synchronize visible mouth and jaw articulation precisely to the "
            "supplied audio. Mouth movement must begin with the first audible speech phoneme, follow the timing and "
            "cadence of the audio without anticipation or lag, and settle closed immediately after the final spoken "
            "phoneme. Do not add unscripted pre-speech or post-speech mouth movement."
        ),
    ]
    if dialogue:
        parts.append(
            f'The spoken line is: "{dialogue[:500]}". Match the supplied audio exactly; use the text only to guide '
            "meaning, expression, and articulation."
        )
    if emotion:
        parts.append(
            f"The active speaker's emotional delivery is {emotion}; express it naturally without exaggeration."
        )
    parts.extend(
        [
            (
                f"{active_name} should use expressive eyes, believable facial emotion, subtle head movement, "
                "realistic breathing, restrained upper-body motion, and context-appropriate conversational gestures."
            ),
            (
                f"{listener_name} must remain silent with the mouth closed, maintain natural attention toward "
                f"{active_name}, and show only subtle listener reactions such as eye movement, a small nod, or "
                "a restrained facial response."
            ),
            (
                "Preserve both people's identities, facial structure, hairstyle, clothing, body proportions, "
                "relative position, lighting, and background geometry. Keep their physical boundaries coherent even "
                "when they are close together. The environment should feel alive through subtle ambient motion while "
                "its layout stays stable. Do not merge faces, hair, shoulders, arms, clothing, or bodies. No face "
                "swapping, extra limbs, body warping, distorted hands, exaggerated gestures, sudden camera motion, "
                "background morphing, or unstable perspective."
            ),
        ]
    )
    return " ".join(part.strip() for part in parts if _clean(part))[:3000]


async def compile_children_performant(
    *,
    context: FusionSceneContext,
    face_client,
    audio_client,
    headers: dict[str, str],
    external_provider_ok: bool,
    request_nonce_by_turn: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Compile canonical child requests with bounded parallel media resolution."""

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
            *(
                load_audio(media_id)
                for media_id in {
                    str(turn.audio_media_id): turn.audio_media_id
                    for turn in context.turns
                }.values()
            ),
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
        face_url = (
            face_urls[str(shared_scene["shared_scene_media_id"])]
            if shared_scene
            else face_urls[str(turn.face_media_id)]
        )
        audio_url = audio_urls[str(turn.audio_media_id)]
        video: dict[str, Any] = {"aspect_ratio": aspect_ratio}
        if turn.duration_hint_ms and turn.duration_hint_ms > 0:
            video["duration_sec"] = max(1, min(30, int(round(turn.duration_hint_ms / 1000.0))))
        if turn.emotion_code:
            video["emotion"] = turn.emotion_code

        turn_key = str(turn.dialogue_turn_id)
        request_nonce = _clean((request_nonce_by_turn or {}).get(turn_key))
        provider_options: dict[str, Any] = {}
        provider_name = "veed_fabric"
        performance_prompt = prompt
        listener = None
        camera_mode = None
        camera_plan_source = None

        if shared_scene:
            provider_name = "omnihuman"
            listener = next(
                (
                    candidate
                    for candidate in context.turns
                    if candidate.participant_id != turn.participant_id
                ),
                None,
            )
            if listener is None:
                raise RuntimeError("shared_scene_listener_required")

            camera_mode, camera_plan_source = _director_camera_mode(context, shared_scene, turn)
            performance_prompt = _shared_scene_performance_prompt(
                context,
                shared_scene,
                turn,
                scene_prompt=prompt,
                camera_mode=camera_mode,
            )
            active_coordinates = _speaker_coordinates(shared_scene, turn.participant_id)
            listener_coordinates = _speaker_coordinates(shared_scene, listener.participant_id)

            provider_options.update(
                {
                    "conversation_mode": "shared_scene",
                    "shared_scene_media_id": shared_scene["shared_scene_media_id"],
                    "shared_scene_dimensions": {
                        "width": int(shared_scene["image_width"]),
                        "height": int(shared_scene["image_height"]),
                    },
                    "shared_scene_video_style": shared_scene["video_style"],
                    "camera_mode": camera_mode,
                    "camera_plan_source": camera_plan_source,
                    "longform_profile": "talking_video",
                    "quality_tier": "premium",
                    "provider_hint": "omnihuman",
                    "fusion_provider": "omnihuman",
                    "presenter_provider": "omnihuman",
                    "resolution": shared_scene["resolution"],
                    "turbo_mode": False,
                    "aspect_ratio": aspect_ratio,
                    "prompt": performance_prompt,
                    "active_speaker_coordinates": active_coordinates,
                    "listener_speaker_coordinates": listener_coordinates,
                    "speaker_mask_cache_key": (
                        f"{shared_scene['shared_scene_media_id']}:{turn.participant_id}"
                    ),
                    "speaker_mask_strategy": "protect_listener",
                    "lipsync_quality_mode": "strict",
                    "background_motion_mode": "ambient",
                    "ambient_motion_plan": _ambient_motion_plan(context),
                }
            )

        if performance_prompt:
            video["prompt"] = performance_prompt

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
                "provider_hint": "omnihuman" if shared_scene else provider_name,
                "quality_tier": "premium" if shared_scene else None,
                "execution_provider_family": "omnihuman" if shared_scene else None,
                "shared_scene_video_style": (
                    shared_scene.get("video_style") if shared_scene else None
                ),
                "shared_scene_camera_mode": camera_mode,
                "camera_plan_source": camera_plan_source,
                "active_speaker_name": turn.display_name if shared_scene else None,
                "listener_name": (
                    getattr(listener, "display_name", None)
                    if shared_scene and listener is not None
                    else None
                ),
            },
        }
        if shared_scene:
            payload["quality_tier"] = "premium"
            payload["longform_profile"] = "talking_video"
            payload["performance_prompt"] = performance_prompt
        if request_nonce:
            provider_options["v3_request_nonce"] = request_nonce
        if provider_options:
            payload["provider_options"] = provider_options

        children.append(
            {
                "dialogue_turn_id": turn_key,
                "participant_id": str(turn.participant_id),
                "display_name": turn.display_name,
                "sequence_no": turn.sequence_no,
                "face_media_id": str(turn.face_media_id) if turn.face_media_id is not None else None,
                "shared_scene_media_id": shared_scene["shared_scene_media_id"] if shared_scene else None,
                "audio_media_id": str(turn.audio_media_id),
                "aspect_ratio": aspect_ratio,
                "camera_mode": camera_mode,
                "camera_plan_source": camera_plan_source,
                "payload": payload,
            }
        )

    return children


__all__ = [
    "compile_children_performant",
    "_input_concurrency",
    "_scene_aspect_ratio",
    "_assert_conversation_mode_supported",
    "_speaker_coordinates",
    "_speaker_position_label",
    "_director_camera_mode",
    "_ambient_motion_plan",
    "_shared_scene_performance_prompt",
]
