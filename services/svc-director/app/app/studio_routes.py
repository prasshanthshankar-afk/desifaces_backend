from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from df_contracts.v3.studio_workflow import ReviewDecision, StudioWorkflowView
from desifaces_shared.v3.story_store import StoryGraphNotFound
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore, StudioWorkflowError

from .security import DirectorAuthContext, get_director_auth
from .studio_workflow import build_story_studio_workflow

router = APIRouter()
store = CanonicalStudioWorkflowStore()


class ReviewIn(BaseModel):
    decision: ReviewDecision
    feedback: str | None = Field(default=None, max_length=12000)


@router.post(
    "/api/director/stories/{story_id}/studio-workflows",
    response_model=StudioWorkflowView,
    status_code=status.HTTP_201_CREATED,
)
async def create_story_studio_workflow(
    story_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = request.app.state.business_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                graph = await request.app.state.story_store.get_story_graph(
                    conn,
                    story_id=story_id,
                    account_id=auth.account_id,
                )
                workflow_id = await build_story_studio_workflow(
                    conn,
                    graph=graph,
                    owner_user_id=auth.user_id,
                    store=store,
                )
                return await store.get_workflow(conn, workflow_id=workflow_id, account_id=auth.account_id)
    except StoryGraphNotFound as exc:
        raise HTTPException(status_code=404, detail="story_not_found") from exc
    except StudioWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/director/studio-workflows/{workflow_id}", response_model=StudioWorkflowView)
async def get_studio_workflow(
    workflow_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        async with request.app.state.business_pool.acquire() as conn:
            return await store.get_workflow(conn, workflow_id=workflow_id, account_id=auth.account_id)
    except StudioWorkflowError as exc:
        raise HTTPException(status_code=404, detail="studio_workflow_not_found") from exc


@router.post("/api/director/studio-reviews/{review_item_id}", response_model=StudioWorkflowView)
async def review_studio_output(
    review_item_id: UUID,
    body: ReviewIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    if body.decision == ReviewDecision.PENDING:
        raise HTTPException(status_code=422, detail="review_decision_must_be_terminal")
    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        workflow_id = await conn.fetchval(
            """
            select w.workflow_id
            from public.v3_studio_review_items r
            join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
            join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
            where r.review_item_id=$1 and w.account_id=$2
            """,
            review_item_id,
            auth.account_id,
        )
        if not workflow_id:
            raise HTTPException(status_code=404, detail="studio_review_not_found")
        try:
            async with conn.transaction():
                await store.review_output(
                    conn,
                    review_item_id=review_item_id,
                    reviewer_user_id=auth.user_id,
                    decision=body.decision,
                    feedback=body.feedback,
                )
                return await store.get_workflow(
                    conn,
                    workflow_id=UUID(str(workflow_id)),
                    account_id=auth.account_id,
                )
        except StudioWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
