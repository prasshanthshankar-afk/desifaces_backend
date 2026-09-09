from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .security import DirectorAuthContext, get_director_auth


router = APIRouter()

AspectRatio = Literal["9:16", "16:9", "1:1"]
_PREVIEWABLE_STATES = frozenset({"pending", "ready", "failed", "rejected"})
_SUPPORTED_STAGES = frozenset({"face", "fusion"})


class StageAspectIn(BaseModel):
    aspect_ratio: AspectRatio = "9:16"


@router.put(
    "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio"
)
async def set_stage_aspect_ratio(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: StageAspectIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                select s.stage_run_id,s.stage_type,s.state,s.metadata_json
                from public.v3_studio_stage_runs s
                join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
                where s.stage_run_id=$1 and s.workflow_id=$2 and w.account_id=$3
                for update of s
                """,
                stage_run_id,
                workflow_id,
                auth.account_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="studio_stage_not_found")
            stage_type = str(row["stage_type"] or "").strip().lower()
            state = str(row["state"] or "").strip().lower()
            if stage_type not in _SUPPORTED_STAGES:
                raise HTTPException(status_code=422, detail="aspect_ratio_not_supported_for_stage")
            if state not in _PREVIEWABLE_STATES:
                raise HTTPException(status_code=409, detail=f"aspect_ratio_locked_for_stage:{stage_type}:{state}")
            metadata = dict(row["metadata_json"] or {})
            metadata["aspect_ratio"] = body.aspect_ratio
            metadata["aspect_ratio_source"] = "user_selected_multi_person"
            await conn.execute(
                """
                update public.v3_studio_stage_runs
                set metadata_json=$2::jsonb,updated_at=now()
                where stage_run_id=$1
                """,
                stage_run_id,
                json.dumps(metadata, ensure_ascii=False),
            )
    return {
        "workflow_id": str(workflow_id),
        "stage_run_id": str(stage_run_id),
        "stage_type": stage_type,
        "state": state,
        "aspect_ratio": body.aspect_ratio,
        "persisted": True,
    }


__all__ = ["router", "StageAspectIn"]
