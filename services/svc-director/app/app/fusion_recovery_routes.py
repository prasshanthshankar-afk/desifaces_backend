from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from desifaces_shared.v3.studio_workflow_store import StudioWorkflowError

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
async def retry_fusion_stitch_only(
    workflow_id: UUID,
    stage_run_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    """Retry only final scene assembly when every child render is already reusable.

    This route deliberately refuses a partial-child failure. Those cases must go
    through normal pricing preview/confirmation so only the failed child is repriced.
    When preview returns no quotes, the resilient execution service has proven that
    every dialogue child has a successful video URL from the prior failed attempt.
    The new attempt therefore performs no svc-fusion child dispatch and creates no
    new child pricing reservation; subsequent sync retries svc-fusion-extension stitch.
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
        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "scene_id": str(context.scene_id),
            "stage_state": "generating",
            "attempt_id": str(attempt_id),
            "attempt_count": attempt_no,
            "attempt_kind": attempt_kind,
            "retry_scope": "stitch_only",
            "preserved_child_count": len(children),
            "new_child_charge": False,
        }
    except HTTPException:
        raise
    except (SceneFusionBridgeError, StudioWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["router"]
