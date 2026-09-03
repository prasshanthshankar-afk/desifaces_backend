from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from df_contracts.v3.studio_workflow import ReviewDecision

from .security import DirectorAuthContext, get_director_auth
from .studio_routes import _advance_after_face_cohort_if_complete, store

router = APIRouter()


class SavedFaceReuseIn(BaseModel):
    media_asset_id: UUID


def _clean(value: Any) -> str:
    return str(value or "").strip()


@router.put(
    "/api/director/studio-workflows/{workflow_id}/participants/{participant_id}/saved-face"
)
async def reuse_saved_face(
    workflow_id: UUID,
    participant_id: UUID,
    body: SavedFaceReuseIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    """Bind an already-owned Face media asset to one Story participant.

    Reuse is intentionally not a generation path: no provider call, pricing quote,
    credit reservation, or new Face job is created. The explicit user selection
    acts as the HITL approval for this participant's Face stage and preserves
    canonical media lineage through media_assets.id.
    """

    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            context = await conn.fetchrow(
                """
                select
                  w.workflow_id,w.account_id,w.project_id,w.current_stage,
                  p.participant_id,p.display_name,
                  s.stage_run_id,s.state as stage_state
                from public.v3_studio_workflows w
                join public.v3_participants p
                  on p.participant_id=$2
                 and p.account_id=w.account_id
                 and p.project_id=w.project_id
                 and p.lifecycle_state='active'
                join public.v3_studio_stage_runs s
                  on s.workflow_id=w.workflow_id
                 and s.stage_type='face'
                 and s.scope_type='participant'
                 and s.participant_id=p.participant_id
                where w.workflow_id=$1 and w.account_id=$3
                """,
                workflow_id,
                participant_id,
                auth.account_id,
            )
            if not context:
                raise HTTPException(status_code=404, detail="participant_face_stage_not_found")
            if _clean(context["current_stage"]) != "face":
                raise HTTPException(status_code=409, detail="saved_face_reuse_not_current")
            if _clean(context["stage_state"]) == "generating":
                raise HTTPException(status_code=409, detail="saved_face_reuse_generation_in_progress")
            if _clean(context["stage_state"]) == "approved":
                current_media = await conn.fetchval(
                    "select primary_face_media_id from public.v3_participants where participant_id=$1",
                    participant_id,
                )
                if current_media and UUID(str(current_media)) == body.media_asset_id:
                    workflow = await store.get_workflow(
                        conn, workflow_id=workflow_id, account_id=auth.account_id
                    )
                    return {
                        "workflow": workflow.model_dump(mode="json"),
                        "participant_id": str(participant_id),
                        "media_asset_id": str(body.media_asset_id),
                        "reused": True,
                        "charged": False,
                    }
                raise HTTPException(status_code=409, detail="participant_face_already_locked")

            asset = await conn.fetchrow(
                """
                select id,user_id,kind,content_type,meta_json
                from public.media_assets
                where id=$1 and user_id=$2
                """,
                body.media_asset_id,
                auth.user_id,
            )
            if not asset:
                raise HTTPException(status_code=404, detail="saved_face_not_found_or_not_owned")

            kind = _clean(asset["kind"]).casefold()
            content_type = _clean(asset["content_type"]).casefold()
            if not (content_type.startswith("image/") or kind in {"image", "face", "face_image"}):
                raise HTTPException(status_code=422, detail="saved_face_asset_must_be_image")

            stage_run_id = UUID(str(context["stage_run_id"]))

            # If a generated candidate is currently awaiting review, choosing a
            # saved Face is an explicit replacement decision. Reject the prior
            # pending candidate(s) first so the stage has one active lineage.
            pending_reviews = await conn.fetch(
                """
                select r.review_item_id
                from public.v3_studio_review_items r
                join public.v3_studio_stage_outputs o
                  on o.stage_run_id=r.stage_run_id and o.media_id=r.media_id
                where r.stage_run_id=$1 and r.decision='pending' and o.is_active=true
                order by r.created_at
                """,
                stage_run_id,
            )
            for row in pending_reviews:
                await store.review_output(
                    conn,
                    review_item_id=UUID(str(row["review_item_id"])),
                    reviewer_user_id=auth.user_id,
                    decision=ReviewDecision.REJECTED,
                    feedback="Replaced by user-selected saved Face",
                )

            review_id = await store.attach_output(
                conn,
                stage_run_id=stage_run_id,
                media_id=body.media_asset_id,
                output_role="approved_face",
            )
            await store.review_output(
                conn,
                review_item_id=review_id,
                reviewer_user_id=auth.user_id,
                decision=ReviewDecision.APPROVED,
                feedback="User selected an existing Face from Saved Work",
            )

            await conn.execute(
                """
                update public.v3_participants
                set primary_face_media_id=$3,
                    metadata_json=coalesce(metadata_json,'{}'::jsonb) || $4::jsonb,
                    updated_at=now()
                where participant_id=$1 and account_id=$2
                """,
                participant_id,
                auth.account_id,
                body.media_asset_id,
                json.dumps(
                    {
                        "face_selection_source": "saved_work",
                        "face_reused_media_asset_id": str(body.media_asset_id),
                    }
                ),
            )
            await conn.execute(
                """
                update public.v3_studio_stage_runs
                set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,
                    updated_at=now()
                where stage_run_id=$1
                """,
                stage_run_id,
                json.dumps(
                    {
                        "face_selection_source": "saved_work",
                        "face_reused_media_asset_id": str(body.media_asset_id),
                        "face_reuse_charged": False,
                    }
                ),
            )

            workflow = await _advance_after_face_cohort_if_complete(
                conn,
                workflow_id=workflow_id,
                account_id=auth.account_id,
            )

    return {
        "workflow": workflow.model_dump(mode="json"),
        "participant_id": str(participant_id),
        "display_name": _clean(context["display_name"]) or "Character",
        "media_asset_id": str(body.media_asset_id),
        "reused": True,
        "charged": False,
    }


__all__ = ["router"]
