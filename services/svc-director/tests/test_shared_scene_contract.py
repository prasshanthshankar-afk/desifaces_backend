from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.shared_scene_routes import (
    NormalizedSpeakerBox,
    SharedSceneConversationIn,
    SharedSceneSpeakerTarget,
)


def test_normalized_speaker_box_accepts_in_canvas_target():
    box = NormalizedSpeakerBox(x=0.10, y=0.20, width=0.30, height=0.40)
    assert box.padding_ratio == 0.10


@pytest.mark.parametrize(
    "payload",
    [
        {"x": 0.80, "y": 0.10, "width": 0.30, "height": 0.30},
        {"x": 0.10, "y": 0.85, "width": 0.30, "height": 0.20},
    ],
)
def test_normalized_speaker_box_rejects_target_outside_canvas(payload):
    with pytest.raises(ValidationError):
        NormalizedSpeakerBox(**payload)


def test_shared_scene_contract_requires_unique_speakers():
    participant_id = uuid4()
    target = SharedSceneSpeakerTarget(
        participant_id=participant_id,
        box=NormalizedSpeakerBox(x=0.1, y=0.1, width=0.3, height=0.5),
    )
    with pytest.raises(ValidationError):
        SharedSceneConversationIn(
            shared_scene_media_id=uuid4(),
            image_width=1920,
            image_height=1080,
            speaker_targets=[target, target],
        )


def test_shared_scene_route_persists_relational_group_photo_lineage():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "shared_scene_routes.py"
    ).read_text(encoding="utf-8")
    assert "'source_image'" in source
    assert "'approved_shared_scene_image'" in source
    assert "v3_studio_stage_inputs" in source


def test_shared_scene_profile_row_lock_avoids_distinct_for_update():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "studio_preflight_routes.py"
    ).read_text(encoding="utf-8")
    assert "select distinct p.participant_id" not in source
    assert "select p.participant_id,p.display_name,p.metadata_json,p.persona_json" in source
    assert "for update of p" in source


def test_shared_scene_people_approval_route_is_persisted_on_workflow():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "studio_preflight_routes.py"
    ).read_text(encoding="utf-8")
    assert "/shared-scene-people-approval" in source
    assert 'metadata["shared_scene_people_approved"] = True' in source
    assert "shared_scene_people_profiles_incomplete" in source


def test_shared_scene_adopts_only_missing_media_lineage():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "shared_scene_routes.py"
    ).read_text(encoding="utf-8")

    assert "and (account_id is null or account_id=$3)" in source
    assert "and (project_id is null or project_id=$4)" in source
    assert "set account_id=coalesce(account_id,$2)" in source
    assert "project_id=coalesce(project_id,$3)" in source
    assert "for update" in source
    assert "where id=$1 and user_id=$2 and account_id=$3" not in source


def test_shared_scene_video_settings_route_persists_pricing_gate():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "shared_scene_routes.py"
    ).read_text(encoding="utf-8")

    assert "/shared-scene-video-settings" in source
    assert 'metadata["shared_scene_provider"] = "omnihuman"' in source
    assert 'metadata["shared_scene_video_style"] = body.motion_mode' in source
    assert 'metadata["shared_scene_motion_mode"] = body.motion_mode' in source
    assert 'metadata["shared_scene_camera_mode"] = body.camera_mode' in source
    assert 'metadata["shared_scene_video_settings_version"] = 2' in source
    assert "shared_scene_video_settings_requires_approved_group_photo" in source
    assert "shared_scene_video_requires_exactly_two_speakers" in source
    assert "len(speaker_targets) != 2" in source
    assert "pricing_ready" in source


def test_shared_scene_fusion_fails_closed_for_non_two_person_video():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "fusion_execution.py"
    ).read_text(encoding="utf-8")

    assert "shared_scene_video_requires_exactly_two_speakers" in source
    assert "len(shared_speaker_ids) != 2" in source


def test_shared_scene_compiler_routes_frozen_omnihuman_provider():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "fusion_input_performance.py"
    ).read_text(encoding="utf-8")

    assert 'provider_name = "omnihuman"' in source
    assert 'provider_name = "kling"' not in source
    assert 'provider_name = "sync3"' not in source
    assert '"quality_tier": "premium"' in source
    assert '"longform_profile": "talking_video"' in source
    assert '"provider_hint": "omnihuman"' in source
    assert '"execution_provider_family": "omnihuman"' in source
    assert '"active_speaker_coordinates": active_coordinates' in source
    assert '"listener_speaker_coordinates": listener_coordinates' in source


def test_shared_scene_compiler_consumes_saved_motion_prompt_and_directs_both_people():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "fusion_input_performance.py"
    ).read_text(encoding="utf-8")

    assert 'metadata.get("shared_scene_video_prompt")' in source
    assert "_shared_scene_performance_prompt" in source
    assert "is the only person speaking" in source
    assert "keep the mouth closed" in source
    assert "context-appropriate conversational gestures" in source
    assert "No face swapping" in source
    assert 'getattr(turn, "dialogue_text", None)' in source
    assert '"prompt": performance_prompt' in source


def test_parent_pricing_requires_seconds_for_shared_scene_only():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "fusion_execution_parent_pricing.py"
    ).read_text(encoding="utf-8")

    assert '"second"' in source
    assert '"minute"' in source
    assert 'get("conversation_mode")' in source
    assert "fusion_parent_pricing_unit_must_be_" in source


def test_shared_scene_supports_static_and_cinematic_camera_contract():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "fusion_input_performance.py"
    ).read_text(encoding="utf-8")

    assert '"static_video"' in source
    assert '"cinematic_video"' in source
    assert '"director_choice"' in source
    for camera in (
        "push_in",
        "push_out",
        "arc_left",
        "arc_right",
        "angle_shift_low_to_eye",
        "angle_shift_high_to_eye",
    ):
        assert f'"{camera}"' in source
    assert "creative_director_scene_direction" in source
    assert "creative_director_contextual_policy_v1" in source


def test_shared_scene_turn_context_carries_dialogue_for_performance_planning():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "fusion_execution.py"
    ).read_text(encoding="utf-8")

    assert "to_jsonb(dt) as turn_json" in source
    assert "dialogue_text: str | None" in source
    assert "dialogue_text=_dialogue_text" in source


def test_shared_scene_quality_release_preserves_setting_and_adds_ambient_motion():
    root = Path(__file__).resolve().parents[1] / "app" / "app"
    execution = (root / "fusion_execution.py").read_text(encoding="utf-8")
    compiler = (root / "fusion_input_performance.py").read_text(encoding="utf-8")
    state_routes = (root / "shared_scene_state_routes.py").read_text(encoding="utf-8")

    assert "scene_setting: dict[str, Any]" in execution
    assert "sc.setting_json" in execution
    assert 'scene_setting=_as_dict(stage["setting_json"])' in execution

    assert "def _ambient_motion_plan" in compiler
    assert "Static Video locks the camera, not the world" in compiler
    assert "do not freeze the world" in compiler
    assert '"background_motion_mode": "ambient"' in compiler
    assert '"ambient_motion_plan": _ambient_motion_plan(context)' in compiler
    assert "small distant non-speaking background people" in state_routes
    assert "never add another foreground subject" in state_routes


def test_shared_scene_quality_release_enforces_strict_per_turn_lipsync_contract():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "fusion_input_performance.py"
    ).read_text(encoding="utf-8")

    assert "Lip-sync is strict" in source
    assert "first audible speech phoneme" in source
    assert "without anticipation or lag" in source
    assert "final spoken phoneme" in source
    assert '"lipsync_quality_mode": "strict"' in source
    assert '"speaker_mask_strategy": "protect_listener"' in source
