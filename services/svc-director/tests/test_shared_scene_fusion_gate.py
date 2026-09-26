from __future__ import annotations

import asyncio

from uuid import uuid4

import pytest

from app.fusion_execution import FusionSceneContext, SceneTurnInput
from app.fusion_input_performance import (
    _assert_conversation_mode_supported,
    _speaker_coordinates,
    compile_children_performant,
)


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


def _shared_context() -> FusionSceneContext:
    base = _context({})
    metadata = {
        "conversation_mode": "shared_scene",
        "shared_scene_media_id": str(uuid4()),
        "shared_scene_dimensions": {"width": 1920, "height": 1080},
        "speaker_targets": {
            str(base.turns[0].participant_id): {
                "point": {"x": 0.20, "y": 0.45},
            },
            str(base.turns[1].participant_id): {
                "box": {
                    "x": 0.60,
                    "y": 0.20,
                    "width": 0.20,
                    "height": 0.50,
                    "padding_ratio": 0.10,
                },
            },
        },
    }
    face_free_turns = tuple(
        SceneTurnInput(**{**turn.__dict__, "face_media_id": None})
        for turn in base.turns
    )
    return FusionSceneContext(
        **{
            **base.__dict__,
            "stage_metadata": metadata,
            "turns": face_free_turns,
        }
    )


def test_existing_fusion_path_is_unchanged_without_conversation_mode():
    assert _assert_conversation_mode_supported(_context({})) is None


def test_shared_scene_contract_resolves_for_sync3():
    context = _shared_context()
    shared = _assert_conversation_mode_supported(context)
    assert shared is not None
    assert shared["image_width"] == 1920
    assert shared["image_height"] == 1080


def test_shared_scene_derives_native_pixel_speaker_center():
    context = _shared_context()
    shared = _assert_conversation_mode_supported(context)
    assert shared is not None
    assert _speaker_coordinates(shared, context.turns[0].participant_id) == [384, 486]
    assert _speaker_coordinates(shared, context.turns[1].participant_id) == [1344, 486]


def test_shared_scene_requires_target_for_every_speaking_participant():
    context = _shared_context()
    metadata = dict(context.stage_metadata)
    metadata["speaker_targets"] = {
        str(context.turns[0].participant_id): metadata["speaker_targets"][str(context.turns[0].participant_id)]
    }
    context = FusionSceneContext(**{**context.__dict__, "stage_metadata": metadata})
    with pytest.raises(RuntimeError, match="shared_scene_speaker_targets_missing"):
        _assert_conversation_mode_supported(context)


def test_shared_scene_compiles_without_individual_face_assets():
    context = _shared_context()
    shared_media_id = context.stage_metadata["shared_scene_media_id"]

    class FaceClient:
        def __init__(self):
            self.media_ids = []

        async def read_url(self, *, headers, media_id):
            self.media_ids.append(str(media_id))
            return "https://example.test/shared-scene.png"

    class AudioClient:
        async def read_url(self, *, headers, media_id):
            return f"https://example.test/{media_id}.wav"

    face_client = FaceClient()
    children = asyncio.run(
        compile_children_performant(
            context=context,
            face_client=face_client,
            audio_client=AudioClient(),
            headers={"Authorization": "Bearer test"},
            external_provider_ok=True,
        )
    )

    assert face_client.media_ids == [shared_media_id]
    assert len(children) == len(context.turns)
    assert all(child["face_media_id"] is None for child in children)
    assert all(child["shared_scene_media_id"] == shared_media_id for child in children)
    assert all(child["payload"]["provider"] == "sync3" for child in children)
    assert all(
        child["payload"]["provider_options"]["conversation_mode"] == "shared_scene"
        for child in children
    )


def test_shared_scene_can_route_to_omnihuman_with_motion_prompt():
    context = _shared_context()
    metadata = {
        **context.stage_metadata,
        "shared_scene_video_provider": "omnihuman_v15",
        "shared_scene_motion_mode": "natural_motion",
        "shared_scene_video_prompt": "Use natural hand gestures and subtle torso movement.",
    }
    context = FusionSceneContext(**{**context.__dict__, "stage_metadata": metadata})

    class FaceClient:
        async def read_url(self, *, headers, media_id):
            return "https://example.test/shared-scene.png"

    class AudioClient:
        async def read_url(self, *, headers, media_id):
            return f"https://example.test/{media_id}.wav"

    children = asyncio.run(
        compile_children_performant(
            context=context,
            face_client=FaceClient(),
            audio_client=AudioClient(),
            headers={"Authorization": "Bearer test"},
            external_provider_ok=True,
        )
    )

    assert all(child["payload"]["provider"] == "omnihuman_v15" for child in children)
    assert all(
        child["payload"]["provider_options"]["prompt"]
        == "Use natural hand gestures and subtle torso movement."
        for child in children
    )
    assert all(
        child["payload"]["provider_options"]["shared_scene_dimensions"]
        == {"width": 1920, "height": 1080}
        for child in children
    )
    assert all(
        len(child["payload"]["provider_options"]["all_speaker_coordinates"]) == 2
        for child in children
    )
