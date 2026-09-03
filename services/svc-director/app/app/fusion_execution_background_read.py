from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from .fusion_execution import (
    _as_dict,
    _clean,
    _latest_attempt,
    _latest_output_review,
    load_fusion_scene_context,
)
from .fusion_execution_parallel_dispatch import (
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,
    _progress_from_result,
)


def _background_enabled() -> bool:
    return str(os.getenv("DF_V3_FUSION_BACKGROUND_COORDINATOR_ENABLED", "0") or "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


class BackgroundFinalizedParallelSceneFusionExecutionService(
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService
):
    """Read-only HTTP sync when server-side V3 scene finalization is enabled.

    Provider polling, the fan-in barrier, stitching and parent pricing commit are owned
    by svc-fusion-extension-stitch-worker. The UI sync endpoint only reads the durable
    state and signs the final video URL once a reviewable output exists. This removes
    the browser/page lifetime from the execution critical path.
    """

    async def sync(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if not _background_enabled():
            return await super().sync(
                pool,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=headers,
            )

        async with pool.acquire() as conn:
            context = await load_fusion_scene_context(
                conn,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
            )
            existing = await _latest_output_review(conn, stage_run_id=stage_run_id)
            latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
            attempt_row = await conn.fetchrow(
                """
                select created_at,metadata_json,error_code,error_message,state
                from public.v3_studio_stage_attempts
                where stage_run_id=$1
                order by attempt_no desc
                limit 1
                """,
                stage_run_id,
            )

        metadata = _as_dict(attempt_row["metadata_json"]) if attempt_row else {}
        children = [dict(item or {}) for item in list(metadata.get("children") or [])]
        parent_pricing = _as_dict(context.stage_metadata.get("fusion_parent_pricing"))
        stage_state = _clean(context.stage_state)

        media_id = None
        video_url = None
        review_item_id = None
        review_decision = None
        if existing:
            media_id = UUID(str(existing["media_id"]))
            video_url = await self.stitch_client.read_url(headers=headers, media_id=media_id)
            review_item_id = str(existing["review_item_id"]) if existing["review_item_id"] else None
            review_decision = str(existing["decision"]) if existing["decision"] else None

        provider_state = None
        if stage_state in {"awaiting_review", "approved"} and media_id is not None:
            provider_state = "succeeded"
        elif stage_state == "failed":
            provider_state = "failed"
        elif latest or children:
            provider_state = "running"

        result: dict[str, Any] = {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "scene_id": str(context.scene_id),
            "provider_state": provider_state,
            "stage_state": stage_state,
            "media_asset_id": str(media_id) if media_id else None,
            "video_url": video_url,
            "review_item_id": review_item_id,
            "review_decision": review_decision,
            "children": children,
            "parent_pricing": _as_dict(parent_pricing.get("pricing")) or parent_pricing,
            "background_finalization": True,
            "error_code": attempt_row["error_code"] if attempt_row else None,
            "error_message": attempt_row["error_message"] if attempt_row else None,
        }
        progress = _progress_from_result(
            result=result,
            metadata=metadata,
            created_at=attempt_row["created_at"] if attempt_row else None,
        )
        coordinator = _as_dict(metadata.get("background_coordinator"))
        phase = _clean(coordinator.get("phase"))
        if phase:
            progress["phase"] = phase
            if phase == "pricing_commit":
                progress["next_phase"] = "human_review"
                progress["message"] = "Your clips are assembled. Finalizing the scene now."
            elif phase == "scene_stitch":
                progress["next_phase"] = "human_review"
                progress["message"] = "All clips are ready. Assembling your scene now."
        return {**result, "progress": progress}


__all__ = ["BackgroundFinalizedParallelSceneFusionExecutionService"]
