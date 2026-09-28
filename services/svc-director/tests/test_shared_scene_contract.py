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
