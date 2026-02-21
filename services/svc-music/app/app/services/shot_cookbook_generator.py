from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class PresetProfile:
    scene: str
    mood: str
    energy: str
    face_mode: str   # mixed|performance|broll_only|no_face|lyric|abstract
    grade: str


def _stable_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _infer_profile_from_name(name: str) -> PresetProfile:
    n = (name or "").lower()
    if "minimal lyric" in n:
        return PresetProfile("lyric", "calm", "mid", "lyric", "lyric_minimal")
    if "kinetic lyric" in n:
        return PresetProfile("lyric", "charged", "peak", "lyric", "lyric_kinetic")
    if "abstract" in n or "visualizer" in n or "particles" in n:
        return PresetProfile("abstract", "calm", "mid", "abstract", "visualizer_particles")
    if "epic trailer" in n:
        return PresetProfile("epic", "epic", "peak", "no_face", "epic_trailer")
    if "patriotic" in n:
        return PresetProfile("nature", "patriotic", "high", "broll_only", "patriotic_landscapes")
    if "temple" in n or "devotional" in n:
        return PresetProfile("temple", "devotional", "low", "mixed", "devotional_serene")
    if "himalayan" in n:
        return PresetProfile("himalayan", "calm", "low", "mixed", "himalayan_soft")
    if "festival" in n or "wedding" in n:
        return PresetProfile("festival", "party", "peak", "mixed", "festival_vibrant")
    if "edm" in n or "club" in n:
        return PresetProfile("edm", "party", "peak", "mixed", "edm_neon")
    if "stadium" in n or "concert" in n:
        return PresetProfile("stadium", "party", "peak", "mixed", "stadium_concert")
    if "urban neon" in n or "tech city" in n:
        return PresetProfile("city", "charged", "high", "mixed", "neon_noir")
    if "rural" in n or "harvest" in n:
        return PresetProfile("rural", "warm", "mid", "mixed", "warm_rural")
    if "monsoon" in n or "rain" in n:
        return PresetProfile("travel", "dramatic", "mid", "mixed", "cinematic_monsoon")
    if "goa" in n or "ocean" in n or "beach" in n:
        return PresetProfile("coastal", "warm", "high", "mixed", "beach_pop")
    if "corporate" in n:
        return PresetProfile("corporate", "calm", "mid", "performance", "corporate_clean")
    if "lo-fi" in n or "coffee" in n:
        return PresetProfile("indie", "cozy", "low", "broll_only", "lofi_night")
    if "sports" in n:
        return PresetProfile("sports", "dramatic", "peak", "mixed", "sports_grit")
    return PresetProfile("city", "charged", "high", "mixed", "cinematic_default")


def _profile_from_preset_row(preset: JsonDict) -> PresetProfile:
    # Prefer DB metadata; fallback only if missing
    scene = preset.get("scene_primary_tag")
    mood = preset.get("mood_tag")
    energy = preset.get("energy_tag")
    face_mode = preset.get("face_mode")
    grade = preset.get("grade")
    if scene and mood and energy and face_mode and grade:
        return PresetProfile(scene, mood, energy, face_mode, grade)
    return _infer_profile_from_name(preset.get("name") or "")


def _base_constraints(profile: PresetProfile) -> JsonDict:
    c = {"no_brand_logos": True, "no_readable_text": True, "no_watermarks": True}
    if profile.face_mode in ("broll_only", "no_face", "lyric", "abstract"):
        c["avoid_faces_in_broll"] = True
    return c


def _look(profile: PresetProfile) -> JsonDict:
    return {
        "grade": profile.grade,
        "contrast": "high" if profile.mood in ("charged", "dramatic", "epic") else "medium",
        "film_grain": "subtle",
        "bloom": "soft" if profile.mood in ("cozy", "devotional") else "controlled",
        "sharpness": "high_subject",
    }


def _edit_defaults(profile: PresetProfile) -> JsonDict:
    pace = "fast" if profile.energy in ("high", "peak") else "medium"
    return {
        "cut_mode": "on_beat",
        "transition_policy": {
            "default": "match_cut",
            "allowed": ["match_cut", "hard_cut", "whip_pan", "flash"],
            "max_non_cut_transitions": 2 if profile.energy == "peak" else 1,
        },
        "pace": pace,
        "audio_sync": {"prefer_downbeats": True, "min_shot_len_sec": 2.5, "max_shot_len_sec": 6.0},
    }


def _camera_archetypes(profile: PresetProfile) -> List[JsonDict]:
    scene = profile.scene

    def drone(kind: str) -> JsonDict:
        return {
            "shot_type": "establishing",
            "camera_angle": "aerial_drone",
            "camera_move": kind,
            "lens_mm": 24,
            "drone": {"altitude_m": 70 if scene in ("city", "stadium", "edm") else 90, "speed_mps": 10},
        }

    def perf_tracking() -> JsonDict:
        return {"shot_type": "medium", "camera_angle": "eye_level", "camera_move": "tracking", "lens_mm": 35}

    def perf_power() -> JsonDict:
        return {"shot_type": "medium_close", "camera_angle": "low_angle", "camera_move": "dolly_in", "lens_mm": 50}

    def insert_top() -> JsonDict:
        return {"shot_type": "insert", "camera_angle": "top_down", "camera_move": "static", "lens_mm": 50}

    def pov_track() -> JsonDict:
        return {"shot_type": "pov", "camera_angle": "eye_level", "camera_move": "tracking", "lens_mm": 24}

    def hero_orbit() -> JsonDict:
        return {"shot_type": "wide", "camera_angle": "low_angle", "camera_move": "orbit", "lens_mm": 24}

    def close_portrait() -> JsonDict:
        return {"shot_type": "closeup", "camera_angle": "eye_level", "camera_move": "static", "lens_mm": 85}

    def broll_whip() -> JsonDict:
        return {"shot_type": "broll", "camera_angle": "high_angle", "camera_move": "whip_pan", "lens_mm": 24}

    def broll_crane() -> JsonDict:
        return {"shot_type": "wide", "camera_angle": "high_angle", "camera_move": "crane_down", "lens_mm": 24}

    def silhouette() -> JsonDict:
        return {"shot_type": "silhouette", "camera_angle": "low_angle", "camera_move": "tilt", "lens_mm": 35}

    return [
        drone("drone_glide"),
        broll_whip(),
        perf_tracking(),
        perf_power(),
        insert_top(),
        broll_crane(),
        pov_track(),
        hero_orbit(),
        close_portrait(),
        broll_whip(),
        silhouette(),
        drone("drone_dive"),
    ]


def _duration_beats_defaults(profile: PresetProfile) -> List[int]:
    # Beats, not seconds. This is what makes it music-aware.
    if profile.energy == "peak":
        return [16, 12, 16, 12, 8, 12, 16, 16, 12, 8, 12, 16]
    if profile.energy == "high":
        return [16, 12, 20, 16, 8, 12, 16, 20, 16, 8, 12, 16]
    if profile.energy == "mid":
        return [16, 16, 24, 16, 8, 16, 16, 24, 16, 8, 16, 16]
    return [16, 16, 24, 20, 8, 16, 16, 24, 20, 8, 16, 16]


def _emotion_motion(profile: PresetProfile, i: int) -> Tuple[str, str]:
    if profile.face_mode in ("lyric", "abstract"):
        return ("calm", "still") if profile.energy in ("low", "mid") else ("charged", "snappy")
    if i == 0:
        return ("awe", "smooth")
    if i in (1, 9):
        return ("impact", "snappy")
    if i in (2, 3, 7):
        emo = "warm" if profile.mood in ("devotional", "cozy") else "confident"
        mot = "cinematic" if profile.energy == "peak" else "gimbal_smooth"
        return (emo, mot)
    if i in (4, 8):
        return ("intimate", "still")
    if i in (5, 6):
        return ("build", "smooth")
    if i == 10:
        return ("mysterious", "smooth")
    return ("resolved", "smooth")


def generate_shot_cookbook_from_preset_row(*, preset: JsonDict, cookbook_version: int = 1) -> JsonDict:
    name = preset["name"]
    profile = _profile_from_preset_row(preset)

    # Lyric / abstract special modes: no camera shoots
    if profile.face_mode == "lyric":
        return {
            "cookbook_version": cookbook_version,
            "preset_name": name,
            "target_defaults": {"aspect_ratio": "16:9", "fps": 30, "look": _look(profile)},
            "global_constraints": _base_constraints(profile),
            "edit_defaults": _edit_defaults(profile),
            "template_clips": [
                {
                    "template_id": f"LYR-{i+1:02d}",
                    "role": "typography",
                    "section_hint": ["intro","intro","verse","verse","pre_chorus","chorus","verse","chorus","bridge","chorus","outro","outro"][i],
                    "duration_beats_default": 16 if i % 2 == 0 else 12,
                    "duration_sec_fallback": 4.0 if i % 2 == 0 else 3.0,
                    "video": {"emotion": "charged" if "kinetic" in profile.grade else "clean",
                              "motion_style": "snappy" if "kinetic" in profile.grade else "still"},
                    "camera": {"shot_type": "typography", "camera_angle": "eye_level", "camera_move": "static", "lens_mm": None},
                    "prompt_hints": ["high readability", "no photos", "no watermarks"],
                }
                for i in range(12)
            ],
        }

    if profile.face_mode == "abstract":
        return {
            "cookbook_version": cookbook_version,
            "preset_name": name,
            "target_defaults": {"aspect_ratio": "16:9", "fps": 30, "look": _look(profile)},
            "global_constraints": _base_constraints(profile),
            "edit_defaults": _edit_defaults(profile),
            "template_clips": [
                {
                    "template_id": f"ABS-{i+1:02d}",
                    "role": "abstract",
                    "section_hint": ["intro","intro","verse","verse","pre_chorus","chorus","verse","chorus","bridge","chorus","outro","outro"][i],
                    "duration_beats_default": 16 if i % 2 == 0 else 12,
                    "duration_sec_fallback": 4.0 if i % 2 == 0 else 3.0,
                    "video": {"emotion": "ambient", "motion_style": "smooth" if i % 2 == 0 else "snappy"},
                    "camera": {"shot_type": "abstract", "camera_angle": "eye_level", "camera_move": "slow_drift", "lens_mm": None},
                    "prompt_hints": ["particles", "beat-react", "no faces", "no text"],
                }
                for i in range(12)
            ],
        }

    cams = _camera_archetypes(profile)
    beats = _duration_beats_defaults(profile)

    section_map = ["intro","intro","verse","verse","verse","pre_chorus","pre_chorus","chorus","chorus","chorus","bridge","outro"]
    roles_map = ["establishing","broll","performance","performance","insert","broll","pov","performance","performance","broll","broll","outro"]

    templates: List[JsonDict] = []
    for i in range(12):
        role = roles_map[i]

        # enforce no-face/broll-only
        if profile.face_mode in ("broll_only", "no_face") and role == "performance":
            role = "broll"
            if (cams[i] or {}).get("shot_type") == "closeup":
                cams[i] = {"shot_type": "silhouette", "camera_angle": "low_angle", "camera_move": "tilt", "lens_mm": 35}

        emo, mot = _emotion_motion(profile, i)
        templates.append(
            {
                "template_id": f"{_stable_hash(name)}-{i+1:02d}",
                "role": role,
                "section_hint": section_map[i],
                "duration_beats_default": int(beats[i]),
                "duration_sec_fallback": (60.0 / 120.0) * float(beats[i]),  # fallback assumes 120 BPM
                "video": {"emotion": emo, "motion_style": mot},
                "camera": cams[i],
                "prompt_hints": (preset.get("tags") or []) + [profile.scene, profile.mood, profile.grade],
            }
        )

    return {
        "cookbook_version": cookbook_version,
        "preset_name": name,
        "target_defaults": {"aspect_ratio": "16:9", "fps": 30, "look": _look(profile)},
        "global_constraints": _base_constraints(profile),
        "edit_defaults": _edit_defaults(profile),
        "template_clips": templates,
    }