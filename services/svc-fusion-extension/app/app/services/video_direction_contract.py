from __future__ import annotations

from typing import Any, Dict, Tuple


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def _choice(value: Any, allowed: set[str], default: str) -> str:
    normalized = _clean(value)
    return normalized if normalized in allowed else default


def normalize_video_direction(tags: Dict[str, Any]) -> Dict[str, str]:
    """Normalize optional customer direction without introducing provider policy.

    Every field has a safe automatic/default path. The browser may expose only a
    subset; native clients may expose even fewer. Provider adapters consume the
    same normalized contract.
    """
    raw = tags.get("video_direction") if isinstance(tags.get("video_direction"), dict) else {}

    performance = _choice(
        raw.get("performance_style") or tags.get("performance_style"),
        {"natural", "calm", "expressive", "energetic"},
        "natural",
    )
    emotion = _choice(
        raw.get("emotion") or tags.get("emotion"),
        {"auto", "warm", "happy", "serious", "dramatic"},
        "auto",
    )
    scene = _choice(
        raw.get("scene_motion") or tags.get("scene_motion"),
        {"auto", "still", "ambient", "lively"},
        "auto",
    )
    hand = _choice(
        raw.get("hand_motion") or tags.get("hand_motion"),
        {"auto", "none", "subtle", "natural", "expressive"},
        "auto",
    )
    body = _choice(
        raw.get("body_motion") or tags.get("body_motion"),
        {"auto", "none", "subtle", "natural", "expressive"},
        "auto",
    )
    camera = _choice(
        raw.get("camera_motion") or tags.get("camera_motion"),
        {"auto", "static", "gentle_push_in", "subtle_drift"},
        "auto",
    )
    delivery = _choice(
        raw.get("delivery_energy") or tags.get("delivery_energy"),
        {"calm", "normal", "energetic"},
        "normal",
    )

    return {
        "performance_style": performance,
        "emotion": emotion,
        "scene_motion": scene,
        "hand_motion": hand,
        "body_motion": body,
        "camera_motion": camera,
        "delivery_energy": delivery,
    }


def direction_prompt(direction: Dict[str, str]) -> str:
    """Create a provider-neutral motion instruction from the normalized plan."""
    parts = [
        f"Use a {direction['performance_style']} speaking performance.",
        (
            "Let facial emotion follow the spoken content naturally."
            if direction["emotion"] == "auto"
            else f"Use a {direction['emotion']} facial and delivery emotion."
        ),
    ]

    hand = direction["hand_motion"]
    if hand == "none":
        parts.append("Keep hand gestures minimal and avoid invented gestures.")
    elif hand == "auto":
        parts.append("Use subtle context-appropriate hand gestures only when natural for the visible framing.")
    else:
        parts.append(f"Use {hand} hand gestures where visible and contextually appropriate.")

    body = direction["body_motion"]
    if body == "none":
        parts.append("Keep body movement steady.")
    elif body == "auto":
        parts.append("Use subtle natural head and body movement appropriate to the speech.")
    else:
        parts.append(f"Use {body} head and body movement while preserving identity and anatomy.")

    scene = direction["scene_motion"]
    if scene == "still":
        parts.append("Keep the existing background visually stable.")
    elif scene == "lively":
        parts.append("Add lively but believable movement to naturally movable background elements while preserving the original scene context.")
    elif scene == "ambient":
        parts.append("Add subtle ambient movement to naturally movable background elements while preserving the original image context.")
    else:
        parts.append("Preserve the original image context and add only subtle natural ambient background movement where the scene supports it; do not replace the setting or invent unrelated objects.")

    camera = direction["camera_motion"]
    if camera == "static":
        parts.append("Keep the camera static.")
    elif camera == "gentle_push_in":
        parts.append("Use a gentle cinematic push-in without reframing the identity.")
    elif camera == "subtle_drift":
        parts.append("Use subtle cinematic camera drift without changing the scene composition materially.")
    else:
        parts.append("Keep camera movement minimal and natural for a talking-video performance.")

    return " ".join(parts)


def apply_video_direction(
    body: Dict[str, Any],
    tags: Dict[str, Any],
    provider_options: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Attach normalized direction to parent execution metadata.

    This function does not select a provider and does not alter pricing. It only
    gives the current/future provider adapter a stable direction plan.
    """
    direction = normalize_video_direction(tags)
    prompt = direction_prompt(direction)

    tags["video_direction"] = direction
    tags["video_direction_prompt"] = prompt
    tags["performance_style"] = direction["performance_style"]
    tags["emotion"] = direction["emotion"]
    tags["scene_motion"] = direction["scene_motion"]
    tags["hand_motion"] = direction["hand_motion"]
    tags["body_motion"] = direction["body_motion"]
    tags["camera_motion"] = direction["camera_motion"]
    tags["delivery_energy"] = direction["delivery_energy"]

    provider_options["video_direction"] = dict(direction)
    provider_options["performance_style"] = direction["performance_style"]
    provider_options["emotion"] = direction["emotion"]
    provider_options["scene_motion"] = direction["scene_motion"]
    provider_options["hand_motion"] = direction["hand_motion"]
    provider_options["body_motion"] = direction["body_motion"]
    provider_options["camera_motion"] = direction["camera_motion"]
    provider_options["delivery_energy"] = direction["delivery_energy"]
    provider_options.setdefault("motion_prompt", prompt)

    # Map explicit scene/camera choices onto the existing generic execution
    # fields. Auto remains provider-neutral and is expressed through the prompt.
    if direction["scene_motion"] == "still":
        body["background_mode"] = "fixed"
    elif direction["scene_motion"] in {"ambient", "lively"}:
        body["background_mode"] = "movement_based"

    if direction["camera_motion"] != "auto":
        body["camera_motion_style"] = direction["camera_motion"]

    return body, tags, provider_options
