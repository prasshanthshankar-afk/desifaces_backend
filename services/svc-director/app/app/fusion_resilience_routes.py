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
    """Retry deterministic scene stitching without re-rendering completed children.

    The logical scene still owns one parent pricing lifecycle. A prior failed attempt
    releases its reservation, so stitch-only recovery obtains one fresh parent quote
    and reservation while preserving every successful child. No child quote, child
    reservation, or child provider dispatch is created by this route.
    """
    headers = _forward_auth(request)
    try:
        async with request.app.state.business_pool.acquire() as conn:
            context, bundle = await fusion_execution.preview(
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

        required_children = int(bundle.get("required_child_count") or 0)
        if required_children != 0:
            raise HTTPException(status_code=409, detail={
                "code": "fusion_child_retry_requires_pricing",
                "message": "One or more dialogue videos still need to be created. Check the scene price to retry only those missing segments under the one parent price.",
                "recoverable": True,
                "action": "check_scene_price",
            })

        parent = dict(bundle.get("parent") or {})
        pricing = dict(parent.get("pricing") or {})
        quote_id = str(pricing.get("quote_id") or "").strip()
        preview_fingerprint = str(pricing.get("preview_fingerprint") or "").strip()
        if not quote_id or not preview_fingerprint:
            raise SceneFusionBridgeError("fusion_parent_pricing_confirmation_required")

        context, attempt_id, attempt_no, attempt_kind, children, parent_pricing = await fusion_execution.dispatch(
            request.app.state.business_pool,
            account_id=auth.account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
            parent_confirmation={
                "quote_id": quote_id,
                "preview_fingerprint": preview_fingerprint,
            },
            child_confirmations=[],
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
        "new_child_dispatches": 0,
        "preserved_child_count": len(children),
        "parent_pricing": parent_pricing,
        "children": children,
    }


__all__ = ["router"]
