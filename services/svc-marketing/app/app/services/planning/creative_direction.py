# services/svc-marketing/app/app/services/planning/creative_direction.py
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional


def _pick(seed: int, key: str, items: List[str]) -> str:
    if not items:
        return ""
    h = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(items)
    return items[idx]


_SCENES = [
    "home_living_room", "home_dining", "home_balcony_rain", "home_bedroom_soft_light", "home_backyard_evening",
    "gym_mirror", "park_morning_walk", "college_corridor", "library_study", "campus_canteen",
    "office_desk", "coworking_startup", "cafe_table", "movie_theatre_lobby",
    "bus_stop_evening", "in_bus_window_seat", "railway_platform", "in_train_window_seat",
    "airport_checkin", "airport_lounge",
    "village_lane_morning", "local_bazaar_market", "community_event_stage",
]

_TIME_OF_DAY = ["morning", "afternoon", "golden_hour", "night_city_lights", "rainy_evening"]

_POSE_ACTION = [
    "talking_to_camera_natural_smile",
    "walking_and_talking_slight_hand_gestures",
    "pointing_to_on_screen_text_overlay",
    "showing_phone_screen_then_back_to_camera",
    "sipping_chai_then_hook_line",
    "laugh_reaction_then_explain",
    "calm_confident_explainer_style",
]

_CAMERA = [
    "handheld_phone_selfie_feel",
    "gentle_push_in",
    "wide_to_mid_cut",
    "mid_to_close_cut",
    "quick_whip_pan_reveal",
    "split_screen_before_after",
]

_ENERGY = ["calm", "warm", "confident", "high_energy", "cinematic", "funny_fast"]

_ATTIRE = [
    "casual_citywear",
    "office_casual",
    "traditional_simple",
    "festival_traditional",
    "college_casual",
    "gym_wear",
]


def build_creative_direction(
    *,
    seed: int,
    country_code: str,
    locale: Optional[str],
    persona: Optional[str],
    industry: Optional[str],
    festival_name: Optional[str],
    motifs: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    motifs = motifs or {}
    scene_primary = _pick(seed, "scene_primary", _SCENES)
    scene_secondary = [_pick(seed, "scene_secondary_1", _SCENES), _pick(seed, "scene_secondary_2", _SCENES)]
    scene_secondary = [s for s in scene_secondary if s and s != scene_primary]

    time_of_day = _pick(seed, "time_of_day", _TIME_OF_DAY)
    pose_action = _pick(seed, "pose_action", _POSE_ACTION)
    camera = [_pick(seed, "camera_1", _CAMERA), _pick(seed, "camera_2", _CAMERA)]
    camera = [c for i, c in enumerate(camera) if c and c not in camera[:i]]

    energy = _pick(seed, "energy", _ENERGY)

    attire = _pick(seed, "attire", _ATTIRE)
    if festival_name:
        # Bias attire towards festival-friendly looks
        attire = "festival_traditional"

    # Optional: motifs-driven color palette / props
    palette = motifs.get("palette") or motifs.get("colors") or []
    if isinstance(palette, str):
        palette = [palette]
    if not isinstance(palette, list):
        palette = []

    props = motifs.get("props") or []
    if isinstance(props, str):
        props = [props]
    if not isinstance(props, list):
        props = []

    # Build prompts consumed by generation
    festival_line = f"Festival vibe: {festival_name}. " if festival_name else ""
    palette_line = f"Color palette: {', '.join([str(x) for x in palette[:4]])}. " if palette else ""
    props_line = f"Props: {', '.join([str(x) for x in props[:4]])}. " if props else ""

    face_prompt = (
        f"{festival_line}{palette_line}{props_line}"
        f"Setting: {scene_primary} at {time_of_day}. "
        f"Attire: {attire}. "
        f"Pose/action: {pose_action}. "
        f"Camera: {', '.join(camera)}. "
        f"Energy: {energy}. "
        f"Country context: {country_code}. "
        f"Ultra-realistic, natural skin texture, authentic local vibe, dynamic background depth, no studio backdrop."
    )

    video_direction = (
        f"{festival_line}"
        f"Background context: {scene_primary} at {time_of_day}. "
        f"Perform with {energy} energy. "
        f"Use natural hand gestures; subtle head movement; friendly eye contact; "
        f"deliver hook in first 2 seconds; pause for emphasis; smile at the end."
    )

    audio_style = {
        "energy": energy,
        "pace": "fast" if energy in ("high_energy", "funny_fast") else "medium" if energy in ("confident", "warm") else "slow",
        "tone": "friendly",
        "locale": locale or "",
    }

    return {
        "scene_primary": scene_primary,
        "scene_secondary": scene_secondary,
        "time_of_day": time_of_day,
        "pose_action": pose_action,
        "camera": camera,
        "energy": energy,
        "attire": attire,
        "palette": palette,
        "props": props,
        "face_prompt": face_prompt,
        "video_direction": video_direction,
        "audio_style": audio_style,
        "persona": persona or "",
        "industry": industry or "",
    }