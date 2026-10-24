"""DEV-only isolated language-version persistence; no provider or media calls."""
from __future__ import annotations
import hashlib
import json
import os
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from .security import DirectorAuthContext, get_director_auth

router = APIRouter()

def _dev():
    if os.getenv("DF_RUNTIME_ENV", "").lower() not in {"dev", "development"}:
        raise HTTPException(status_code=404, detail="dev_only")

class LocalizedTurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    user_confirmed: bool = False

@router.post("/api/director/studio-workflows/{workflow_id}/language-versions/hi-IN")
async def create_hindi_version(workflow_id: UUID, request: Request,
                               auth: DirectorAuthContext = Depends(get_director_auth)):
    _dev()
    async with request.app.state.business_pool.acquire() as conn:
        async with conn.transaction():
            source = await conn.fetchrow(
                """SELECT workflow_id,story_id,account_id,final_media_id
                   FROM public.v3_studio_workflows
                   WHERE workflow_id=$1 AND account_id=$2 FOR SHARE""",
                workflow_id, auth.account_id)
            if not source:
                raise HTTPException(status_code=404, detail="source_workflow_not_found")
            if not source["final_media_id"]:
                raise HTTPException(status_code=409, detail="source_conversation_not_completed")
            turns = await conn.fetch(
                """SELECT dt.turn_id,dt.scene_id,dt.sequence_no,dt.text_value,
                          dt.speaker_participant_id
                   FROM public.v3_dialogue_turns dt
                   JOIN public.v3_scenes sc ON sc.scene_id=dt.scene_id
                   WHERE sc.story_id=$1 AND dt.turn_kind='speech'
                   ORDER BY sc.sequence_no,dt.sequence_no,dt.turn_id""",
                source["story_id"])
            if not turns:
                raise HTTPException(status_code=409, detail="source_dialogue_missing")
            fingerprint = hashlib.sha256(json.dumps([
                [str(t["turn_id"]),str(t["speaker_participant_id"]),t["text_value"]]
                for t in turns
            ],ensure_ascii=False).encode()).hexdigest()
            # Serialize version assignment for this source workflow.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                               "language_version:" + str(workflow_id))
            existing = await conn.fetchrow(
                """SELECT language_version_id,version_no,state
                   FROM public.v3_story_language_versions
                   WHERE source_workflow_id=$1 AND target_locale='hi-IN'
                     AND source_fingerprint=$2 AND state IN ('draft','awaiting_hitl')
                   ORDER BY version_no DESC LIMIT 1""",workflow_id,fingerprint)
            if existing:
                return {"language_version_id":str(existing["language_version_id"]),
                        "target_locale":"hi-IN","version_no":existing["version_no"],
                        "state":existing["state"],"reused_draft":True}
            next_no = await conn.fetchval(
                """SELECT coalesce(max(version_no),0)+1 FROM public.v3_story_language_versions
                   WHERE source_workflow_id=$1 AND target_locale='hi-IN'""",workflow_id)
            version = await conn.fetchval(
                """INSERT INTO public.v3_story_language_versions
                   (source_workflow_id,story_id,account_id,target_locale,source_fingerprint,version_no)
                   VALUES($1,$2,$3,'hi-IN',$4,$5) RETURNING language_version_id""",
                workflow_id,source["story_id"],auth.account_id,fingerprint,next_no)
            await conn.executemany(
                """INSERT INTO public.v3_story_language_turns
                   (language_version_id,source_turn_id,speaker_participant_id,source_text)
                   VALUES($1,$2,$3,$4)""",
                [(version,t["turn_id"],t["speaker_participant_id"],t["text_value"]) for t in turns])
    return {"language_version_id":str(version),"target_locale":"hi-IN",
            "version_no":next_no,"state":"draft","reused_draft":False,
            "source_workflow_unchanged":True,"media_generation_started":False}

@router.get("/api/director/studio-workflows/{workflow_id}/language-versions")
async def list_language_versions(workflow_id: UUID,request:Request,
                                  auth:DirectorAuthContext=Depends(get_director_auth)):
    _dev()
    async with request.app.state.business_pool.acquire() as conn:
        source = await conn.fetchval(
            "SELECT 1 FROM public.v3_studio_workflows WHERE workflow_id=$1 AND account_id=$2",
            workflow_id,auth.account_id)
        if not source:
            raise HTTPException(status_code=404, detail="source_workflow_not_found")
        rows=await conn.fetch(
            """SELECT language_version_id,target_locale,version_no,state,created_at,
                      final_media_id FROM public.v3_story_language_versions
               WHERE source_workflow_id=$1 AND account_id=$2
               ORDER BY created_at DESC""",workflow_id,auth.account_id)
    return {"versions":[{"language_version_id":str(r["language_version_id"]),
                         "target_locale":r["target_locale"],"version_no":r["version_no"],
                         "state":r["state"],"has_final_video":bool(r["final_media_id"])}
                        for r in rows]}

@router.put("/api/director/language-versions/{version_id}/dialogue-turns/{turn_id}")
async def review_localized_turn(version_id:UUID,turn_id:UUID,body:LocalizedTurnIn,
                                request:Request,auth:DirectorAuthContext=Depends(get_director_auth)):
    _dev()
    if not body.user_confirmed:
        raise HTTPException(status_code=422,detail="existing_dialogue_hitl_confirmation_required")
    async with request.app.state.business_pool.acquire() as conn:
        async with conn.transaction():
            version=await conn.fetchrow(
                """SELECT state FROM public.v3_story_language_versions
                   WHERE language_version_id=$1 AND account_id=$2 FOR UPDATE""",
                version_id,auth.account_id)
            if not version:
                raise HTTPException(status_code=404,detail="language_version_not_found")
            if version["state"] not in ("draft","awaiting_hitl"):
                raise HTTPException(status_code=409,detail="localized_dialogue_locked")
            row=await conn.fetchrow(
                """SELECT t.state FROM public.v3_story_language_turns t
                   WHERE t.language_version_id=$1 AND t.source_turn_id=$2 FOR UPDATE""",
                version_id,turn_id)
            if not row:
                raise HTTPException(status_code=404,detail="source_turn_not_in_version")
            if row["state"]=="approved":
                raise HTTPException(status_code=409,detail="localized_turn_already_approved")
            # User-authored text is stored verbatim; no AI rewrite or second review.
            await conn.execute(
                """UPDATE public.v3_story_language_turns
                   SET localized_text=$3,state='approved',approved_at=now(),updated_at=now()
                   WHERE language_version_id=$1 AND source_turn_id=$2""",
                version_id,turn_id,body.text)
            remaining=await conn.fetchval(
                """SELECT count(*) FROM public.v3_story_language_turns
                   WHERE language_version_id=$1 AND state<>'approved'""",version_id)
            await conn.execute(
                """UPDATE public.v3_story_language_versions
                   SET state=$2,updated_at=now() WHERE language_version_id=$1""",
                version_id,"approved" if remaining==0 else "awaiting_hitl")
    return {"language_version_id":str(version_id),"turn_id":str(turn_id),
            "text":body.text,"approved":True,
            "all_dialogue_approved":remaining==0,"media_generation_started":False}
