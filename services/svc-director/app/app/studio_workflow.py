from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from df_contracts.v3.story import DialogueTurnKind, StoryGraph
from df_contracts.v3.studio_workflow import (
    StudioScopeType,
    StudioStageType,
    StudioWorkflowState,
)
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore


async def build_story_studio_workflow(
    conn,
    *,
    graph: StoryGraph,
    owner_user_id: UUID,
    store: CanonicalStudioWorkflowStore,
) -> UUID:
    """Create the durable review-gated execution graph for one Story.

    Face is participant-scoped. Audio is speech-turn-scoped. Fusion is scene-scoped.
    For multi-scene stories a final story assembly/review stage depends on every
    approved scene Fusion stage.
    """
    workflow_id = await store.create_workflow(
        conn,
        account_id=graph.project.account_id,
        owner_user_id=owner_user_id,
        project_id=graph.project.project_id,
        story_id=graph.story.story_id,
        metadata={"workflow_kind": "face_audio_fusion_story", "hitl_required": True},
    )

    face_by_participant: dict[UUID, UUID] = {}
    for participant in graph.participants:
        face_by_participant[participant.participant_id] = await store.add_stage(
            conn,
            workflow_id=workflow_id,
            stage_type=StudioStageType.FACE,
            scope_type=StudioScopeType.PARTICIPANT,
            participant_id=participant.participant_id,
            metadata={"review_required": True, "output_role": "approved_face"},
        )

    audio_by_turn: dict[UUID, UUID] = {}
    audio_by_scene: dict[UUID, list[UUID]] = defaultdict(list)
    for turn in graph.dialogue_turns:
        if turn.kind != DialogueTurnKind.SPEECH:
            continue
        stage_id = await store.add_stage(
            conn,
            workflow_id=workflow_id,
            stage_type=StudioStageType.AUDIO,
            scope_type=StudioScopeType.DIALOGUE_TURN,
            dialogue_turn_id=turn.turn_id,
            metadata={
                "review_required": True,
                "speaker_participant_id": str(turn.speaker_participant_id),
                "output_role": "approved_audio",
            },
        )
        audio_by_turn[turn.turn_id] = stage_id
        audio_by_scene[turn.scene_id].append(stage_id)
        if turn.speaker_participant_id is not None:
            await store.add_dependency(
                conn,
                parent_stage_run_id=face_by_participant[turn.speaker_participant_id],
                child_stage_run_id=stage_id,
            )

    members_by_scene: dict[UUID, list[UUID]] = defaultdict(list)
    for membership in graph.scene_participants:
        members_by_scene[membership.scene_id].append(membership.participant_id)

    fusion_by_scene: dict[UUID, UUID] = {}
    for scene in graph.scenes:
        fusion_stage = await store.add_stage(
            conn,
            workflow_id=workflow_id,
            stage_type=StudioStageType.FUSION,
            scope_type=StudioScopeType.SCENE,
            scene_id=scene.scene_id,
            metadata={"review_required": True, "output_role": "approved_scene_video"},
        )
        fusion_by_scene[scene.scene_id] = fusion_stage
        for participant_id in members_by_scene.get(scene.scene_id, ()):
            await store.add_dependency(
                conn,
                parent_stage_run_id=face_by_participant[participant_id],
                child_stage_run_id=fusion_stage,
            )
        for audio_stage in audio_by_scene.get(scene.scene_id, ()):
            await store.add_dependency(
                conn,
                parent_stage_run_id=audio_stage,
                child_stage_run_id=fusion_stage,
            )

    if len(graph.scenes) > 1:
        final_stage = await store.add_stage(
            conn,
            workflow_id=workflow_id,
            stage_type=StudioStageType.STORY_FINAL,
            scope_type=StudioScopeType.STORY,
            metadata={"review_required": True, "output_role": "approved_story_video"},
        )
        for fusion_stage in fusion_by_scene.values():
            await store.add_dependency(
                conn,
                parent_stage_run_id=fusion_stage,
                child_stage_run_id=final_stage,
            )

    await store.set_workflow_state(
        conn,
        workflow_id=workflow_id,
        state=StudioWorkflowState.ACTIVE,
        current_stage=StudioStageType.FACE,
    )
    return workflow_id
