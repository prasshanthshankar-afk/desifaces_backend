from __future__ import annotations

from uuid import uuid4
import inspect

import pytest
from pydantic import ValidationError

from app.shared_scene_routes import (
    NormalizedSpeakerBox,
    SharedSceneConversationIn,
    SharedSceneDraftIn,
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


def test_shared_scene_draft_allows_partial_mapping():
    p1 = uuid4()
    p2 = uuid4()
    draft = SharedSceneDraftIn(
        shared_scene_media_id=uuid4(),
        image_width=1536,
        image_height=1024,
        speaker_targets=[
            SharedSceneSpeakerTarget(
                participant_id=p1,
                point={"x": 0.25, "y": 0.30},
            )
        ],
    )
    assert len(draft.speaker_targets) == 1
    assert draft.speaker_targets[0].participant_id == p1
    assert p2 != p1


def test_shared_scene_draft_requires_dimension_pair():
    with pytest.raises(ValidationError):
        SharedSceneDraftIn(
            shared_scene_media_id=uuid4(),
            image_width=1536,
            image_height=None,
            speaker_targets=[],
        )


def test_shared_scene_approval_clears_draft_metadata():
    from app.shared_scene_routes import set_shared_scene_conversation

    source = inspect.getsource(set_shared_scene_conversation)
    assert "shared_scene_draft_media_id" in source
    assert "metadata.pop(draft_key, None)" in source


def test_shared_scene_binding_allows_same_account_cross_project_group_photo_reuse():
    from app.shared_scene_routes import set_shared_scene_conversation

    source = inspect.getsource(set_shared_scene_conversation).lower()
    assert "(account_id=$3 or account_id is null)" in source
    assert "(project_id is null or project_id=$4)" not in source
    assert "group photos are reusable saved media" in source


def test_shared_scene_draft_allows_same_account_cross_project_group_photo_reuse():
    from app.shared_scene_routes import set_shared_scene_draft

    source = inspect.getsource(set_shared_scene_draft).lower()
    assert "(account_id=$3 or account_id is null)" in source
    assert "(project_id is null or project_id=$4)" not in source


def test_shared_scene_people_approval_is_durable_workflow_metadata():
    from app.shared_scene_routes import approve_shared_scene_people

    source = inspect.getsource(approve_shared_scene_people)
    assert 'metadata["shared_scene_people_approved"] = True' in source
    assert '"shared_scene_people_approved_participant_ids"' in source
