from __future__ import annotations

from uuid import uuid4

import pytest

from app.fusion_execution import FusionSceneContext, SceneTurnInput
from app.fusion_input_performance import _assert_conversation_mode_supported


def _context(metadata: dict) -> FusionSceneContext:
    participant_a = uuid4()
    participant_b = uuid4()
    return FusionSceneContext(
        workflow_id=uuid4(),
        stage_run_id=uuid4(),
        account_id=uuid4(),
        owner_user_id=uuid4(),
        project_id=uuid4(),
        story_id=uuid4(),
        scene_id=uuid4(),
        stage_state="ready",
        stage_metadata=metadata,
        scene_title="Conversation",
        scene_summary="Two people discuss a topic.",
        scene_direction={},
        turns=(
            SceneTurnInput(
                dialogue_turn_id=uuid4(),
                sequence_no=0,
                participant_id=participant_a,
                display_name="Speaker A",
                face_media_id=uuid4(),
                audio_media_id=uuid4(),
                emotion_code=None,
                duration_hint_ms=None,
            ),
            SceneTurnInput(
                dialogue_turn_id=uuid4(),
                sequence_no=1,
                participant_id=participant_b,
                display_name="Speaker B",
                face_media_id=uuid4(),
                audio_media_id=uuid4(),
                emotion_code=None,
                duration_hint_ms=None,
            ),
        ),
    )


def test_existing_fusion_path_is_unchanged_without_conversation_mode():
    _assert_conversation_mode_supported(_context({}))


def test_shared_scene_fails_closed_before_media_pipeline_is_installed():
    context = _context({})
    targets = {
        str(turn.participant_id): {
            "x": 0.1,
            "y": 0.1,
            "width": 0.3,
            "height": 0.5,
            "padding_ratio": 0.1,
        }
        for turn in context.turns
    }
    context = _context(
        {
            "conversation_mode": "shared_scene",
            "shared_scene_media_id": str(uuid4()),
            "speaker_targets": targets,
        }
    )
    # Rebuild targets for the participant ids in the final immutable context.
    metadata = dict(context.stage_metadata)
    metadata["speaker_targets"] = {
        str(turn.participant_id): {
            "x": 0.1,
            "y": 0.1,
            "width": 0.3,
            "height": 0.5,
            "padding_ratio": 0.1,
        }
        for turn in context.turns
    }
    context = FusionSceneContext(
        **{**context.__dict__, "stage_metadata": metadata}
    )

    with pytest.raises(RuntimeError, match="shared_scene_media_pipeline_required"):
        _assert_conversation_mode_supported(context)


def test_shared_scene_requires_target_for_every_speaking_participant():
    context = _context(
        {
            "conversation_mode": "shared_scene",
            "shared_scene_media_id": str(uuid4()),
            "speaker_targets": {},
        }
    )
    with pytest.raises(RuntimeError, match="shared_scene_speaker_targets_missing"):
        _assert_conversation_mode_supported(context)
