from __future__ import annotations

from uuid import uuid4
import inspect

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


def test_shared_scene_profile_lock_query_avoids_distinct_for_update():
    from app.studio_preflight_routes import set_shared_scene_participant_profile

    source = inspect.getsource(set_shared_scene_participant_profile).lower()
    assert "for update of p" in source
    assert "select distinct p.participant_id" not in source


def test_shared_scene_binding_accepts_same_user_legacy_face_asset_and_adopts_lineage():
    from app.shared_scene_routes import set_shared_scene_conversation

    source = inspect.getsource(set_shared_scene_conversation).lower()
    assert "(account_id=$3 or account_id is null)" in source
    assert "project_id=coalesce(project_id,$3)" in source
    assert "where id=$1 and user_id=$4 and account_id is null" in source


def test_shared_scene_binding_accepts_uploaded_source_image_assets():
    from app.shared_scene_routes import set_shared_scene_conversation

    source = inspect.getsource(set_shared_scene_conversation).lower()
    assert "'source_image'" in source
    assert "shared_scene_media_not_owned_active_image" in source
