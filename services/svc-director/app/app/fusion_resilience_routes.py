from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from .fusion_execution import SceneFusionBridgeError
from .security import DirectorAuthContext, get_director_auth
from .studio_e2e_routes import fusion_execution

router = APIRouter()


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
    """Retry deterministic scene stitching only when every child is reusable.

    The canonical performant/resilient Fusion execution instance is reused here.
    First run its non-billable preview: an empty quote list is the proof that every
    dialogue child already has a reusable successful video. If any child still needs
    work, the user must return to normal Check Price so only those failed/missing
    children receive new quotes. The stitch-only attempt performs no svc-fusion child
    dispatch and therefore creates no new child charge.
    """
    headers = _forward_auth(request)
    try:
        async with request.app.state.business_pool.acquire() as conn:
            context, quotes = await fusion_execution.preview(
                conn,
                account_id=auth.account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=headers,
                external_provider_ok=True,
            )

        if context.stage_state != "failed":
            raise HTTPException(status_code=409, detail={
                "code": "fusion_stitch_retry_requires_failed_scene",
                "message": "Scene assembly retry is only available after a failed scene attempt.",
                "recoverable": True,
                "action": "reload_scene",
            })
        if quotes:
            raise HTTPException(status_code=409, detail={
                "code": "fusion_child_retry_requires_pricing",
                "message": "One or more dialogue videos still need to be created. Check the scene price to retry only those segments.",
                "recoverable": True,
                "action": "check_scene_price",
            })

        context, attempt_id, attempt_no, attempt_kind, children = await fusion_execution.dispatch(
            request.app.state.business_pool,
            account_id=auth.account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
            confirmations=[],
            external_provider_ok=True,
        )
    except HTTPException:
        raise
    except SceneFusionBridgeError as exc:
        raise HTTPException(status_code=409, detail={
            "code": str(exc),
            "message": "The scene could not be prepared for a stitch-only retry. Reload the scene and try again.",
            "recoverable": True,
            "action": "reload_scene",
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
        "preserved_child_count": len(children),
        "children": children,
    }


__all__ = ["router"]
