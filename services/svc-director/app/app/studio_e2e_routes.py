from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from df_contracts.v3.studio_workflow import StudioWorkflowView
from desifaces_shared.v3.studio_workflow_store import StudioWorkflowError

from .audio_execution import ParticipantAudioBridgeError, ParticipantAudioExecutionService
from .config import settings
from .fusion_execution import SceneFusionBridgeError, SceneFusionExecutionService
from .security import DirectorAuthContext, get_director_auth
from .studio_progression import advance_studio_workflow
from .studio_routes import store

router = APIRouter()

audio_execution = ParticipantAudioExecutionService(
    audio_base_url=settings.DF_AUDIO_BASE_URL,
    store=store,
)
fusion_execution = SceneFusionExecutionService(
    face_base_url=settings.DF_FACE_BASE_URL,
    audio_base_url=settings.DF_AUDIO_BASE_URL,
    fusion_base_url=settings.DF_FUSION_BASE_URL,
    fusion_extension_base_url=settings.DF_FUSION_EXTENSION_BASE_URL,
    store=store,
)


class AudioDispatchIn(BaseModel):
    quote_id: str = Field(min_length=1, max_length=300)
    preview_fingerprint: str | None = Field(default=None, max_length=500)
    user_confirmed: bool = True


class FusionPreviewIn(BaseModel):
    external_provider_ok: bool = False


class FusionTurnConfirmation(BaseModel):
    dialogue_turn_id: UUID
    request_nonce: str = Field(min_length=1, max_length=300)
    quote_id: str = Field(min_length=1, max_length=300)
    preview_fingerprint: str | None = Field(default=None, max_length=500)


class FusionDispatchIn(BaseModel):
    confirmations: list[FusionTurnConfirmation] = Field(min_length=1, max_length=200)
    external_provider_ok: bool = False
    user_confirmed: bool = True


def _forward_auth(request: Request) -> dict[str, str]:
    value = str(request.headers.get("authorization") or "").strip()
    if not value:
        raise HTTPException(status_code=401, detail="authorization_header_required")
    return {"Authorization": value}


@router.post(
    "/api/director/studio-workflows/{workflow_id}/audio-stages/{stage_run_id}/pricing-preview"
)
async def preview_audio_stage(
    workflow_id: UUID,
    stage_run_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        async with request.app.state.business_pool.acquire() as conn:
            context, studio_input, pricing = await audio_execution.preview(
                conn,
                account_id=auth.account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=_forward_auth(request),
            )
        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "dialogue_turn_id": str(context.dialogue_turn_id),
            "participant_id": str(context.participant_id),
            "display_name": context.display_name,
            "stage_state": context.stage_state,
            "studio_input": studio_input,
            "pricing": pricing,
        }
    except (ParticipantAudioBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/director/studio-workflows/{workflow_id}/audio-stages/{stage_run_id}/dispatch"
)
async def dispatch_audio_stage(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: AudioDispatchIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    if body.user_confirmed is not True:
        raise HTTPException(status_code=422, detail="audio_pricing_user_confirmation_required")
    try:
        context, job_id, attempt_no, attempt_kind, attempt_id = await audio_execution.dispatch(
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
            "dialogue_turn_id": str(context.dialogue_turn_id),
            "participant_id": str(context.participant_id),
            "display_name": context.display_name,
            "audio_job_id": job_id,
            "stage_state": "generating",
            "attempt_id": str(attempt_id),
            "attempt_count": attempt_no,
            "attempt_kind": attempt_kind,
        }
    except (ParticipantAudioBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/director/studio-workflows/{workflow_id}/audio-stages/{stage_run_id}/sync"
)
async def sync_audio_stage(
    workflow_id: UUID,
    stage_run_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        result = await audio_execution.sync(
            request.app.state.business_pool,
            account_id=auth.account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=_forward_auth(request),
        )
        async with request.app.state.business_pool.acquire() as conn:
            workflow = await advance_studio_workflow(
                conn,
                store=store,
                workflow_id=workflow_id,
                account_id=auth.account_id,
            )
        return {**result, "workflow": workflow.model_dump(mode="json")}
    except (ParticipantAudioBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/pricing-preview"
)
async def preview_fusion_stage(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: FusionPreviewIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        async with request.app.state.business_pool.acquire() as conn:
            context, quotes = await fusion_execution.preview(
                conn,
                account_id=auth.account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=_forward_auth(request),
                external_provider_ok=body.external_provider_ok,
            )
        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "scene_id": str(context.scene_id),
            "stage_state": context.stage_state,
            "render_strategy": "dialogue_turn_segments_then_stitch",
            "turn_count": len(context.turns),
            "children": quotes,
        }
    except (SceneFusionBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/dispatch"
)
async def dispatch_fusion_stage(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: FusionDispatchIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    if body.user_confirmed is not True:
        raise HTTPException(status_code=422, detail="fusion_pricing_user_confirmation_required")
    if body.external_provider_ok is not True:
        raise HTTPException(status_code=422, detail="fusion_external_provider_consent_required")
    try:
        context, attempt_id, attempt_no, attempt_kind, children = await fusion_execution.dispatch(
            request.app.state.business_pool,
            account_id=auth.account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=_forward_auth(request),
            confirmations=[item.model_dump(mode="json") for item in body.confirmations],
            external_provider_ok=body.external_provider_ok,
        )
        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "scene_id": str(context.scene_id),
            "stage_state": "generating",
            "attempt_id": str(attempt_id),
            "attempt_count": attempt_no,
            "attempt_kind": attempt_kind,
            "children": children,
        }
    except (SceneFusionBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/sync"
)
async def sync_fusion_stage(
    workflow_id: UUID,
    stage_run_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        result = await fusion_execution.sync(
            request.app.state.business_pool,
            account_id=auth.account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=_forward_auth(request),
        )
        async with request.app.state.business_pool.acquire() as conn:
            workflow = await advance_studio_workflow(
                conn,
                store=store,
                workflow_id=workflow_id,
                account_id=auth.account_id,
            )
        return {**result, "workflow": workflow.model_dump(mode="json")}
    except (SceneFusionBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/director/studio-workflows/{workflow_id}/advance",
    response_model=StudioWorkflowView,
)
async def advance_workflow(
    workflow_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        async with request.app.state.business_pool.acquire() as conn:
            return await advance_studio_workflow(
                conn,
                store=store,
                workflow_id=workflow_id,
                account_id=auth.account_id,
            )
    except StudioWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["router"]
