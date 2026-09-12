from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from df_contracts.v3.studio_workflow import (
    ReviewDecision,
    StudioStageType,
    StudioWorkflowState,
    StudioWorkflowView,
)
from desifaces_shared.v3.story_store import StoryGraphNotFound
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore, StudioWorkflowError

from .config import settings
from .face_execution import ParticipantFaceExecutionService
from .participant_face import ParticipantFaceBridgeError, promote_approved_face_candidate
from .security import DirectorAuthContext, get_director_auth
from .studio_workflow import build_direct_studio_workflow, build_story_studio_workflow

router = APIRouter()
store = CanonicalStudioWorkflowStore()
face_execution = ParticipantFaceExecutionService(face_base_url=settings.DF_FACE_BASE_URL, store=store)


class ReviewIn(BaseModel):
    decision: ReviewDecision
    feedback: str | None = Field(default=None, max_length=12000)


class FaceDispatchIn(BaseModel):
    quote_id: str = Field(min_length=1, max_length=300)
    preview_fingerprint: str | None = Field(default=None, max_length=500)
    user_confirmed: bool = True


def _forward_auth(request: Request) -> dict[str, str]:
    value = str(request.headers.get("authorization") or "").strip()
    if not value:
        raise HTTPException(status_code=401, detail="authorization_header_required")
    return {"Authorization": value}


async def _advance_after_face_cohort_if_complete(conn, *, workflow_id: UUID, account_id: UUID) -> StudioWorkflowView:
    view = await store.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)
    face_cohort = next((item for item in view.cohorts if item.cohort_key == "face_cast"), None)
    if face_cohort and face_cohort.satisfied and view.current_stage == StudioStageType.FACE:
        await store.set_workflow_state(
            conn,
            workflow_id=workflow_id,
            state=StudioWorkflowState.ACTIVE,
            current_stage=StudioStageType.AUDIO,
        )
        view = await store.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)
    return view


@router.post(
    "/api/director/projects/{project_id}/participants/{participant_id}/studio-workflows",
    response_model=StudioWorkflowView,
    status_code=status.HTTP_201_CREATED,
)
async def create_direct_studio_workflow(
    project_id: UUID,
    participant_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        participant = await conn.fetchrow(
            """select participant_id from public.v3_participants
            where participant_id=$1 and project_id=$2 and account_id=$3 and lifecycle_state='active'""",
            participant_id, project_id, auth.account_id,
        )
        if not participant:
            raise HTTPException(status_code=404, detail="participant_not_found")

        existing = await conn.fetchval(
            """select workflow_id from public.v3_studio_workflows
            where account_id=$1 and project_id=$2 and story_id is null
              and state in ('draft','active','awaiting_review')
              and metadata_json->>'workflow_kind'='face_audio_fusion_direct'
            order by created_at desc limit 1""",
            auth.account_id, project_id,
        )
        if existing:
            return await store.get_workflow(
                conn, workflow_id=UUID(str(existing)), account_id=auth.account_id,
            )

        try:
            async with conn.transaction():
                workflow_id = await build_direct_studio_workflow(
                    conn,
                    account_id=auth.account_id,
                    owner_user_id=auth.user_id,
                    project_id=project_id,
                    participant_id=participant_id,
                    store=store,
                )
                return await store.get_workflow(conn, workflow_id=workflow_id, account_id=auth.account_id)
        except StudioWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


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
            existing = await conn.fetchval(
                """select workflow_id from public.v3_studio_workflows
                where account_id=$1 and story_id=$2
                  and state in ('draft','active','awaiting_review')
                  and metadata_json->>'workflow_kind'='face_audio_fusion_story'
                order by created_at desc limit 1""",
                auth.account_id, story_id,
            )
            if existing:
                return await store.get_workflow(
                    conn, workflow_id=UUID(str(existing)), account_id=auth.account_id,
                )

            async with conn.transaction():
                graph = await request.app.state.story_store.get_story_graph(
                    conn, story_id=story_id, account_id=auth.account_id,
                )
                workflow_id = await build_story_studio_workflow(
                    conn, graph=graph, owner_user_id=auth.user_id, store=store,
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


@router.post("/api/director/studio-workflows/{workflow_id}/face-stages/{stage_run_id}/pricing-preview")
async def preview_participant_face(
    workflow_id: UUID,
    stage_run_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        async with request.app.state.business_pool.acquire() as conn:
            context, studio_input, pricing = await face_execution.preview(
                conn,
                account_id=auth.account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=_forward_auth(request),
            )
        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "participant_id": str(context.participant_id),
            "display_name": context.display_name,
            "stage_state": context.stage_state,
            "studio_input": studio_input,
            "pricing": pricing,
        }
    except ParticipantFaceBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/director/studio-workflows/{workflow_id}/face-stages/{stage_run_id}/dispatch", status_code=status.HTTP_202_ACCEPTED)
async def dispatch_participant_face(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: FaceDispatchIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    if not body.user_confirmed:
        raise HTTPException(status_code=422, detail="face_pricing_confirmation_required")
    try:
        context, job_id, attempt_count, attempt_kind, attempt_id = await face_execution.dispatch(
            request.app.state.business_pool,
            account_id=auth.account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=_forward_auth(request),
            quote_id=body.quote_id,
            preview_fingerprint=body.preview_fingerprint,
        )
        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "participant_id": str(context.participant_id),
            "display_name": context.display_name,
            "face_job_id": job_id,
            "stage_state": "generating",
            "attempt_id": str(attempt_id),
            "attempt_count": attempt_count,
            "attempt_kind": attempt_kind,
        }
    except (ParticipantFaceBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/director/studio-workflows/{workflow_id}/face-stages/{stage_run_id}/sync")
async def sync_participant_face(
    workflow_id: UUID,
    stage_run_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        result = await face_execution.sync(
            request.app.state.business_pool,
            account_id=auth.account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=_forward_auth(request),
        )
        async with request.app.state.business_pool.acquire() as conn:
            workflow = await _advance_after_face_cohort_if_complete(
                conn, workflow_id=workflow_id, account_id=auth.account_id,
            )
        return {**result, "workflow": workflow.model_dump(mode="json")}
    except (ParticipantFaceBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
        review_target = await conn.fetchrow(
            """select w.workflow_id,r.stage_run_id,r.media_id,s.stage_type,s.scope_type
            from public.v3_studio_review_items r
            join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
            join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
            where r.review_item_id=$1 and w.account_id=$2""",
            review_item_id, auth.account_id,
        )
        if not review_target:
            raise HTTPException(status_code=404, detail="studio_review_not_found")
        workflow_id = UUID(str(review_target["workflow_id"]))
        stage_run_id = UUID(str(review_target["stage_run_id"]))
        media_id = UUID(str(review_target["media_id"]))
        try:
            async with conn.transaction():
                await store.review_output(
                    conn,
                    review_item_id=review_item_id,
                    reviewer_user_id=auth.user_id,
                    decision=body.decision,
                    feedback=body.feedback,
                )

                # Generated Face images are candidates until HITL approval. Only a
                # completed/approved Face stage may promote the accepted candidate
                # into v3_participants.primary_face_media_id.
                if (
                    body.decision == ReviewDecision.APPROVED
                    and str(review_target["stage_type"]) == "face"
                    and str(review_target["scope_type"]) == "participant"
                ):
                    stage_state = await conn.fetchval(
                        "select state from public.v3_studio_stage_runs where stage_run_id=$1",
                        stage_run_id,
                    )
                    if str(stage_state) == "approved":
                        await promote_approved_face_candidate(
                            conn,
                            account_id=auth.account_id,
                            stage_run_id=stage_run_id,
                            media_asset_id=media_id,
                        )

                return await _advance_after_face_cohort_if_complete(
                    conn, workflow_id=workflow_id, account_id=auth.account_id,
                )
        except (StudioWorkflowError, ParticipantFaceBridgeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
