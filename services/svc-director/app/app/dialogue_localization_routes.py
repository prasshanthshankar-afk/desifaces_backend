"""DEV-only persistence endpoint for the existing dialogue HITL decision.

This is NOT a second approval gate. The caller presents localized text in
its existing review screen; the user explicitly approves or edits it there.
"""
from __future__ import annotations
import json
import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .security import DirectorAuthContext, get_director_auth

router = APIRouter()


class LocalizationDecisionIn(BaseModel):
    source_text: str = Field(min_length=1, max_length=8000)
    target_locale: str = Field(min_length=2, max_length=40)
    text: str = Field(min_length=1, max_length=8000)
    user_confirmed: bool = False


@router.put(
    "/api/director/studio-workflows/{workflow_id}/dialogue-turns/{turn_id}/localization"
)
async def save_approved_localization(
    workflow_id: UUID,
    turn_id: UUID,
    body: LocalizationDecisionIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    if os.getenv("DF_RUNTIME_ENV", "").lower() not in {"dev", "development"}:
        raise HTTPException(status_code=404, detail="dev_only")
    if body.user_confirmed is not True:
        raise HTTPException(status_code=422, detail="dialogue_hitl_confirmation_required")
    async with request.app.state.business_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """select s.stage_run_id,s.state,s.metadata_json,dt.text_value
                   from public.v3_studio_stage_runs s
                   join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
                   join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
                   where w.workflow_id=$1 and w.account_id=$2
                     and s.dialogue_turn_id=$3 and s.stage_type='audio'
                     and s.scope_type='dialogue_turn'
                   for update of s""",
                workflow_id, auth.account_id, turn_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="dialogue_turn_not_found")
            if str(row["state"]) not in {"pending", "ready", "failed", "rejected"}:
                raise HTTPException(status_code=409, detail="audio_stage_locked")
            if str(row["text_value"] or "").strip() != body.source_text.strip():
                raise HTTPException(status_code=409, detail="dialogue_source_changed")
            meta = row["metadata_json"] or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            meta["approved_localization"] = {
                "approved_by_user": True,
                "dialogue_turn_id": str(turn_id),
                "source_text": body.source_text.strip(),
                "target_locale": body.target_locale.strip(),
                "text": body.text,
            }
            await conn.execute(
                """update public.v3_studio_stage_runs
                   set metadata_json=$2::jsonb,updated_at=now()
                   where stage_run_id=$1""",
                row["stage_run_id"],
                json.dumps(meta, ensure_ascii=False),
            )
    return {
        "dialogue_turn_id": str(turn_id),
        "status": "approved",
        "target_locale": body.target_locale,
        "text": body.text,
    }
