from __future__ import annotations

from typing import Any
from uuid import UUID

from df_contracts.v3.story import StoryGraph


async def load_story_studio_projection(
    conn,
    *,
    graph: StoryGraph,
    account_id: UUID,
    active_scene_id: UUID | None = None,
    active_participant_id: UUID | None = None,
) -> tuple[dict[str, str], tuple[dict[str, Any], ...]]:
    """Return UI generation-state map and focus-aware Assistant stage context."""
    workflow = await conn.fetchrow(
        """select workflow_id,state,current_stage,final_media_id,updated_at
        from public.v3_studio_workflows
        where story_id=$1 and account_id=$2 and state<>'canceled'
        order by updated_at desc,created_at desc limit 1""",
        graph.story.story_id, account_id,
    )
    if not workflow:
        return {}, ()

    rows = await conn.fetch(
        """select s.stage_run_id,s.stage_type,s.scope_type,s.participant_id,s.scene_id,s.dialogue_turn_id,
                  s.state,s.generation_request_id,s.generation_job_id,s.updated_at,
                  (select count(*) from public.v3_studio_stage_outputs o
                    where o.stage_run_id=s.stage_run_id and o.is_active=true) active_output_count,
                  (select count(*) from public.v3_studio_review_items r
                    join public.v3_studio_stage_outputs o
                      on o.stage_run_id=r.stage_run_id and o.media_id=r.media_id and o.is_active=true
                    where r.stage_run_id=s.stage_run_id and r.decision='pending') pending_review_count,
                  (select count(*) from public.v3_studio_review_items r
                    join public.v3_studio_stage_outputs o
                      on o.stage_run_id=r.stage_run_id and o.media_id=r.media_id and o.is_active=true
                    where r.stage_run_id=s.stage_run_id and r.decision='approved') approved_output_count
           from public.v3_studio_stage_runs s
           where s.workflow_id=$1
           order by s.created_at,s.stage_run_id""",
        workflow["workflow_id"],
    )

    scene_members: dict[UUID, set[UUID]] = {}
    participant_scenes: dict[UUID, set[UUID]] = {}
    for item in graph.scene_participants:
        scene_members.setdefault(item.scene_id, set()).add(item.participant_id)
        participant_scenes.setdefault(item.participant_id, set()).add(item.scene_id)
    turn_scene = {turn.turn_id: turn.scene_id for turn in graph.dialogue_turns}
    turn_speaker = {turn.turn_id: turn.speaker_participant_id for turn in graph.dialogue_turns}

    states: dict[str, str] = {}
    context: list[dict[str, Any]] = []
    for row in rows:
        stage_type = str(row["stage_type"])
        stage_state = str(row["state"])
        participant_id = UUID(str(row["participant_id"])) if row["participant_id"] else None
        scene_id = UUID(str(row["scene_id"])) if row["scene_id"] else None
        turn_id = UUID(str(row["dialogue_turn_id"])) if row["dialogue_turn_id"] else None

        if stage_type == "face" and participant_id:
            states[f"participant:{participant_id}"] = stage_state
        elif stage_type == "audio" and turn_id:
            states[f"turn:{turn_id}"] = stage_state
        elif stage_type == "fusion" and scene_id:
            states[f"scene:{scene_id}"] = stage_state

        include = True
        if active_scene_id is not None:
            include = (
                scene_id == active_scene_id
                or (turn_id is not None and turn_scene.get(turn_id) == active_scene_id)
                or (participant_id is not None and participant_id in scene_members.get(active_scene_id, set()))
            )
        if include and active_participant_id is not None:
            include = (
                participant_id == active_participant_id
                or (turn_id is not None and turn_speaker.get(turn_id) == active_participant_id)
                or (scene_id is not None and scene_id in participant_scenes.get(active_participant_id, set()))
            )

        if include:
            context.append({
                "workflow_id": str(workflow["workflow_id"]),
                "workflow_state": str(workflow["state"]),
                "current_stage": workflow["current_stage"],
                "stage_run_id": str(row["stage_run_id"]),
                "stage_type": stage_type,
                "scope_type": str(row["scope_type"]),
                "participant_id": str(participant_id) if participant_id else None,
                "scene_id": str(scene_id) if scene_id else None,
                "dialogue_turn_id": str(turn_id) if turn_id else None,
                "stage_state": stage_state,
                "generation_request_id": str(row["generation_request_id"]) if row["generation_request_id"] else None,
                "generation_job_id": str(row["generation_job_id"]) if row["generation_job_id"] else None,
                "active_output_count": int(row["active_output_count"] or 0),
                "pending_review_count": int(row["pending_review_count"] or 0),
                "approved_output_count": int(row["approved_output_count"] or 0),
            })
    return states, tuple(context)
