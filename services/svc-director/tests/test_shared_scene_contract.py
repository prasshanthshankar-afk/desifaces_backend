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
    assert 'metadata["shared_scene_motion_mode"] = body.motion_mode' in source
    assert 'metadata["shared_scene_video_settings_version"] = 1' in source
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
