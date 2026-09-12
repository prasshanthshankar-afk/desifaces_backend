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


def _metadata(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value or {})
    except Exception:
        return {}


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
    """Persist one consistent generation format for the workflow stage family.

    Face stages share one Face format across the cast. Fusion stages share one
    Scene format across scenes. A selection propagates to every still-previewable
    sibling stage. Once any sibling has entered generation/review/approval, its
    durable format locks that stage family so refreshes or direct API calls cannot
    create mixed-aspect output or quote/render drift.
    """
    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow(
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
            if not target:
                raise HTTPException(status_code=404, detail="studio_stage_not_found")

            stage_type = str(target["stage_type"] or "").strip().lower()
            state = str(target["state"] or "").strip().lower()
            if stage_type not in _SUPPORTED_STAGES:
                raise HTTPException(status_code=422, detail="aspect_ratio_not_supported_for_stage")
            if state not in _PREVIEWABLE_STATES:
                raise HTTPException(
                    status_code=409,
                    detail=f"aspect_ratio_locked_for_stage:{stage_type}:{state}",
                )

            siblings = await conn.fetch(
                """
                select s.stage_run_id,s.state,s.metadata_json
                from public.v3_studio_stage_runs s
                join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
                where s.workflow_id=$1 and w.account_id=$2 and s.stage_type=$3
                order by s.stage_run_id
                for update of s
                """,
                workflow_id,
                auth.account_id,
                stage_type,
            )

            locked_ratios: set[str] = set()
            for sibling in siblings:
                sibling_state = str(sibling["state"] or "").strip().lower()
                if sibling_state in _PREVIEWABLE_STATES:
                    continue
                meta = _metadata(sibling["metadata_json"])
                ratio = str(meta.get("aspect_ratio") or "9:16").strip()
                locked_ratios.add(ratio if ratio in {"9:16", "16:9", "1:1"} else "9:16")

            if locked_ratios and (locked_ratios != {body.aspect_ratio}):
                locked = ",".join(sorted(locked_ratios))
                raise HTTPException(
                    status_code=409,
                    detail=f"aspect_ratio_locked_for_workflow:{stage_type}:{locked}",
                )

            propagated = 0
            for sibling in siblings:
                sibling_state = str(sibling["state"] or "").strip().lower()
                if sibling_state not in _PREVIEWABLE_STATES:
                    continue
                metadata = _metadata(sibling["metadata_json"])
                metadata["aspect_ratio"] = body.aspect_ratio
                metadata["aspect_ratio_source"] = "user_selected_multi_person"
                await conn.execute(
                    """
                    update public.v3_studio_stage_runs
                    set metadata_json=$2::jsonb,updated_at=now()
                    where stage_run_id=$1
                    """,
                    sibling["stage_run_id"],
                    json.dumps(metadata, ensure_ascii=False),
                )
                propagated += 1

    return {
        "workflow_id": str(workflow_id),
        "stage_run_id": str(stage_run_id),
        "stage_type": stage_type,
        "state": state,
        "aspect_ratio": body.aspect_ratio,
        "persisted": True,
        "propagated_stage_count": propagated,
        "workflow_consistent": True,
    }


__all__ = ["router", "StageAspectIn"]
