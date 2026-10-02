from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app" / "app"


def test_shared_scene_state_contract_is_registered_and_db_authoritative():
    runtime = (ROOT / "studio_routes_runtime.py").read_text(encoding="utf-8")
    state = (ROOT / "shared_scene_state_routes.py").read_text(encoding="utf-8")
    preflight = (ROOT / "studio_preflight_routes.py").read_text(encoding="utf-8")

    assert "shared_scene_state_routes" in runtime
    assert "/shared-scene-state" in state
    assert "/shared-scene-source" in state
    assert "shared_scene_source_mode" in state
    assert "shared_scene_state_version" in state
    assert "shared_scene_people_snapshot" in preflight


def test_shared_scene_state_exposes_one_phase_and_one_next_action():
    state = (ROOT / "shared_scene_state_routes.py").read_text(encoding="utf-8")

    for phase in (
        '"people"',
        '"group_photo_source"',
        '"group_photo_prepare"',
        '"group_photo_map"',
        '"group_photo_approve"',
        '"group_photo_complete"',
        '"audio"',
        '"video"',
        '"final"',
    ):
        assert phase in state

    assert '"next_action": next_action' in state
    assert '"allowed_actions": allowed_actions' in state
    assert "shared_scene_generate_requires_at_least_two_speakers" in state
    assert '"generate_supported": len(speakers) >= 2' in state
    assert '"supported": video_supported' in state
    assert '"max_people": 2' in state
    assert '"video_unavailable_for_group_size"' in state
    assert "shared_scene_source_locked_after_photo_selection" in state


def test_shared_scene_group_photo_spec_and_draft_are_durable_commands():
    state = (ROOT / "shared_scene_state_routes.py").read_text(encoding="utf-8")
    scene = (ROOT / "shared_scene_routes.py").read_text(encoding="utf-8")

    assert "/shared-scene-group-photo-spec" in state
    assert "shared_scene_group_photo_spec" in state
    assert "generation_input" in state
    assert "/shared-scene-draft" in scene
    assert "shared_scene_draft_media_id" in scene
    assert '"speaker_targets": targets' in state
    assert '"dimensions": dimensions or None' in state


def test_legacy_approved_snapshot_is_repaired_before_group_photo_progression():
    state = (ROOT / "shared_scene_state_routes.py").read_text(encoding="utf-8")

    assert '"legacy_missing"' in state
    assert "shared_scene_people_snapshot" in state
    assert "shared_scene_people_snapshot_repair_requires_complete_profiles" in state
    assert "The first explicit" in state
    assert "post-approval source command atomically freezes" in state


def test_group_photo_generation_input_honors_face_prompt_limit():
    state = (ROOT / "shared_scene_state_routes.py").read_text(encoding="utf-8")

    assert "max_face_prompt_chars = 1500" in state
    assert "trim only" in state
    assert "shared_scene_group_photo_face_prompt_contract_exceeded" in state
    assert 'if len(speakers) < 2:' in state
    assert '"participant_count": len(speakers)' in state
