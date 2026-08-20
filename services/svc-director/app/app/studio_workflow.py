from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from df_contracts.v3.story import DialogueTurnKind, StoryGraph
from df_contracts.v3.studio_workflow import StudioScopeType, StudioStageType, StudioWorkflowState
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore


async def build_direct_studio_workflow(
    conn,
    *,
    account_id: UUID,
    owner_user_id: UUID,
    project_id: UUID,
    participant_id: UUID,
    store: CanonicalStudioWorkflowStore,
) -> UUID:
    """Preserve today's direct one-person Face -> Audio -> Fusion journey.

    Each Studio remains independently reviewable. Audio cannot start until Face
    is approved; Fusion cannot start until both Face and Audio are approved.
    Story is intentionally optional for this path.
    """
    workflow_id = await store.create_workflow(
        conn,
        account_id=account_id,
        owner_user_id=owner_user_id,
        project_id=project_id,
        metadata={
            "workflow_kind": "face_audio_fusion_direct",
            "hitl_required": True,
            "execution_manifest": {
                "face_gate_scope": "single_participant",
                "required_face_participant_ids": [str(participant_id)],
                "audio_requires_complete_face_cohort": True,
            },
        },
    )
    face_stage = await store.add_stage(
        conn, workflow_id=workflow_id, stage_type=StudioStageType.FACE,
        scope_type=StudioScopeType.PARTICIPANT, participant_id=participant_id,
        metadata={"review_required": True, "output_role": "approved_face", "cohort": "face_cast"},
    )
    audio_stage = await store.add_stage(
        conn, workflow_id=workflow_id, stage_type=StudioStageType.AUDIO,
        scope_type=StudioScopeType.PARTICIPANT, participant_id=participant_id,
        metadata={"review_required": True, "output_role": "approved_audio", "requires_cohort": "face_cast"},
    )
    fusion_stage = await store.add_stage(
        conn, workflow_id=workflow_id, stage_type=StudioStageType.FUSION,
        scope_type=StudioScopeType.PARTICIPANT, participant_id=participant_id,
        metadata={"review_required": True, "output_role": "approved_video"},
    )
    await store.add_dependency(conn, parent_stage_run_id=face_stage, child_stage_run_id=audio_stage)
    await store.add_dependency(conn, parent_stage_run_id=face_stage, child_stage_run_id=fusion_stage)
    await store.add_dependency(conn, parent_stage_run_id=audio_stage, child_stage_run_id=fusion_stage)
    await store.set_workflow_state(
        conn, workflow_id=workflow_id, state=StudioWorkflowState.ACTIVE,
        current_stage=StudioStageType.FACE,
    )
    return workflow_id


async def build_story_studio_workflow(
    conn,
    *,
    graph: StoryGraph,
    owner_user_id: UUID,
    store: CanonicalStudioWorkflowStore,
) -> UUID:
    """Create the durable review-gated execution graph for one Story.

    Face is participant-scoped. Audio is speech-turn-scoped. Fusion is scene-scoped.
    Multi-scene stories add a final assembly/review stage after approved scene videos.

    Canonical V3 invariant:
      * successful Face outputs are independent, durable, billable and retry-safe;
      * no successful/approved Face independently advances the Story;
      * every Story Audio stage depends on the complete required Face cast cohort;
      * approved Faces stay locked while only failed/rejected members are retried.
    """
    required_participant_ids = tuple(participant.participant_id for participant in graph.participants)
    workflow_id = await store.create_workflow(
        conn,
        account_id=graph.project.account_id,
        owner_user_id=owner_user_id,
        project_id=graph.project.project_id,
        story_id=graph.story.story_id,
        metadata={
            "workflow_kind": "face_audio_fusion_story",
            "hitl_required": True,
            "execution_manifest": {
                "face_gate_scope": "story_cast",
                "required_face_participant_ids": [str(value) for value in required_participant_ids],
                "required_face_count": len(required_participant_ids),
                "audio_requires_complete_face_cohort": True,
                "retry_policy": "retry_failed_or_rejected_slot_only",
                "approved_output_policy": "lock_and_reuse",
            },
        },
    )

    face_by_participant: dict[UUID, UUID] = {}
    for participant in graph.participants:
        face_by_participant[participant.participant_id] = await store.add_stage(
            conn, workflow_id=workflow_id, stage_type=StudioStageType.FACE,
            scope_type=StudioScopeType.PARTICIPANT, participant_id=participant.participant_id,
            metadata={
                "review_required": True,
                "output_role": "approved_face",
                "cohort": "face_cast",
                "cohort_scope": "story",
            },
        )

    # Freeze the complete Face cohort before any Story Audio can advance. This is
    # intentionally broader than speaker-only dependency: if Ananya is approved
    # but Ravi is failed/pending/rejected, *all* Story Audio remains blocked.
    required_face_stages = tuple(face_by_participant.values())

    audio_by_scene: dict[UUID, list[UUID]] = defaultdict(list)
    for turn in graph.dialogue_turns:
        if turn.kind != DialogueTurnKind.SPEECH:
            continue
        if turn.speaker_participant_id is None:
            raise ValueError(f"speech_turn_requires_speaker:{turn.turn_id}")
        stage_id = await store.add_stage(
            conn, workflow_id=workflow_id, stage_type=StudioStageType.AUDIO,
            scope_type=StudioScopeType.DIALOGUE_TURN,
            participant_id=turn.speaker_participant_id,
            dialogue_turn_id=turn.turn_id,
            metadata={
                "review_required": True,
                "speaker_participant_id": str(turn.speaker_participant_id),
                "output_role": "approved_audio",
                "requires_cohort": "face_cast",
            },
        )
        audio_by_scene[turn.scene_id].append(stage_id)
        for face_stage_id in required_face_stages:
            await store.add_dependency(
                conn,
                parent_stage_run_id=face_stage_id,
                child_stage_run_id=stage_id,
            )

    members_by_scene: dict[UUID, list[UUID]] = defaultdict(list)
    for membership in graph.scene_participants:
        members_by_scene[membership.scene_id].append(membership.participant_id)

    fusion_by_scene: dict[UUID, UUID] = {}
    for scene in graph.scenes:
        fusion_stage = await store.add_stage(
            conn, workflow_id=workflow_id, stage_type=StudioStageType.FUSION,
            scope_type=StudioScopeType.SCENE, scene_id=scene.scene_id,
            metadata={"review_required": True, "output_role": "approved_scene_video"},
        )
        fusion_by_scene[scene.scene_id] = fusion_stage
        for participant_id in members_by_scene.get(scene.scene_id, ()):
            await store.add_dependency(
                conn, parent_stage_run_id=face_by_participant[participant_id], child_stage_run_id=fusion_stage,
            )
        for audio_stage in audio_by_scene.get(scene.scene_id, ()):
            await store.add_dependency(conn, parent_stage_run_id=audio_stage, child_stage_run_id=fusion_stage)

    if len(graph.scenes) > 1:
        final_stage = await store.add_stage(
            conn, workflow_id=workflow_id, stage_type=StudioStageType.STORY_FINAL,
            scope_type=StudioScopeType.STORY,
            metadata={"review_required": True, "output_role": "approved_story_video"},
        )
        for fusion_stage in fusion_by_scene.values():
            await store.add_dependency(conn, parent_stage_run_id=fusion_stage, child_stage_run_id=final_stage)

    await store.set_workflow_state(
        conn, workflow_id=workflow_id, state=StudioWorkflowState.ACTIVE,
        current_stage=StudioStageType.FACE,
    )
    return workflow_id
