"""DEV-only, read-only preparation for a new locale version of a completed story.

Never updates the source workflow, approved faces, audio, pricing or media.
A later explicitly confirmed creation operation must provision distinct
locale-scoped stages; existing mutable conversation-settings is NOT reused.
"""
from __future__ import annotations

import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from .security import DirectorAuthContext, get_director_auth

router = APIRouter()


@router.get("/api/director/studio-workflows/{workflow_id}/language-versions/hi-IN/prepare")
async def prepare_hindi_language_version(
    workflow_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    if os.getenv("DF_RUNTIME_ENV", "").lower() not in {"dev", "development"}:
        raise HTTPException(status_code=404, detail="dev_only")
    async with request.app.state.business_pool.acquire() as conn:
        workflow = await conn.fetchrow(
            """select workflow_id,story_id,project_id,state,metadata_json,final_media_id
               from public.v3_studio_workflows
               where workflow_id=$1 and account_id=$2""",
            workflow_id, auth.account_id,
        )
        if not workflow:
            raise HTTPException(status_code=404, detail="workflow_not_found")
        if not workflow["final_media_id"]:
            raise HTTPException(status_code=409, detail="source_conversation_not_completed")
        turns = await conn.fetch(
            """select dt.turn_id,dt.sequence_no,dt.text_value,dt.speaker_participant_id,
                      p.display_name,dt.scene_id
               from public.v3_dialogue_turns dt
               join public.v3_scenes sc on sc.scene_id=dt.scene_id
               join public.v3_participants p on p.participant_id=dt.speaker_participant_id
               where sc.story_id=$1
               order by dt.scene_id,dt.sequence_no,dt.turn_id""",
            workflow["story_id"],
        )
        if not turns:
            raise HTTPException(status_code=409, detail="source_dialogue_missing")
        preserved = await conn.fetch(
            """select stage_run_id,stage_type,state
               from public.v3_studio_stage_runs where workflow_id=$1""",
            workflow_id,
        )
    return {
        "source_workflow_id": str(workflow_id),
        "story_id": str(workflow["story_id"]),
        "target_locale": "hi-IN",
        "operation": "prepare_only",
        "new_generation_started": False,
        "single_dialogue_hitl": True,
        "source_unchanged": True,
        "source_turns": [
            {"turn_id": str(t["turn_id"]), "sequence_no": t["sequence_no"],
             "speaker_participant_id": str(t["speaker_participant_id"]),
             "speaker": t["display_name"], "scene_id": str(t["scene_id"]),
             "text": t["text_value"]}
            for t in turns
        ],
        "preserved_stage_count": len(preserved),
        "next_action": "create_isolated_language_version_then_localize_and_review",
    }
