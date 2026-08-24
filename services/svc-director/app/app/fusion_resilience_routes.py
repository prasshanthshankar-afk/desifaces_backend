from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from .config import settings
from .fusion_execution import SceneFusionBridgeError
from .fusion_execution_resilient import ResilientSceneFusionExecutionService
from .security import DirectorAuthContext, get_director_auth
from .studio_routes import store

router = APIRouter()

fusion_execution = ResilientSceneFusionExecutionService(
    face_base_url=settings.DF_FACE_BASE_URL,
    audio_base_url=settings.DF_AUDIO_BASE_URL,
    fusion_base_url=settings.DF_FUSION_BASE_URL,
    fusion_extension_base_url=settings.DF_FUSION_EXTENSION_BASE_URL,
    store=store,
)


def _forward_auth(request: Request) -> dict[str, str]:
    value = str(request.headers.get("authorization") or "").strip()
    if not value:
        raise HTTPException(status_code=401, detail="authorization_header_required")
    return {"Authorization": value}


@router.post(
    "/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/retry-stitch"
)
async def retry_scene_stitch(
    workflow_id: UUID,
    stage_run_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    """Retry only deterministic scene stitching when all child renders are reusable.

    No svc-fusion pricing preview or child provider dispatch occurs on this path. The
    resilient execution service verifies that the prior failed attempt contains a
    successful video URL for every dialogue child before allowing an empty
    confirmation bundle.
    """
    try:
        context, attempt_id, attempt_no, attempt_kind, children = await fusion_execution.dispatch(
            request.app.state.business_pool,
            account_id=auth.account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=_forward_auth(request),
            confirmations=[],
            external_provider_ok=True,
        )
    except SceneFusionBridgeError as exc:
        code = str(exc)
        message = (
            "The completed dialogue videos cannot be stitched yet. Retry scene pricing for the failed segment."
            if code == "fusion_pricing_confirmation_bundle_mismatch"
            else "The scene could not be prepared for a stitch retry."
        )
        raise HTTPException(status_code=409, detail={
            "code": code,
            "message": message,
            "recoverable": True,
            "action": "retry_scene",
        }) from exc

    return {
        "workflow_id": str(workflow_id),
        "stage_run_id": str(stage_run_id),
        "scene_id": str(context.scene_id),
        "stage_state": "generating",
        "attempt_id": str(attempt_id),
        "attempt_count": attempt_no,
        "attempt_kind": attempt_kind,
        "retry_scope": "stitch_only",
        "new_child_charges": 0,
        "children": children,
    }


__all__ = ["router"]
