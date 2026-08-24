from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from .fusion_execution import (
    SceneFusionBridgeError,
    _as_dict,
    _clean,
    _latest_attempt,
    _video_url_from_status,
    load_fusion_scene_context,
)
from .fusion_execution_parent_pricing import ParentPricedSceneFusionExecutionService


_TERMINAL_SUCCESS = {"succeeded", "completed", "complete", "ready"}
_TERMINAL_FAILURE = {"failed", "canceled", "cancelled", "blocked"}
_ACTIVE = {"queued", "running", "processing", "submitted", "pending", "finalizing", "unknown", ""}


def _payload_turn_id(payload: dict[str, Any]) -> str:
    for key in ("provider_options", "tags"):
        block = _as_dict(payload.get(key))
        for ctx_key in ("billing_context", "pricing_context"):
            ctx = _as_dict(block.get(ctx_key))
            value = _clean(ctx.get("segment_id"))
            if value:
                return value
        value = _clean(block.get("segment_id"))
        if value:
            return value
    return ""


def _payload_parent_stage_id(payload: dict[str, Any]) -> str:
    for key in ("provider_options", "tags"):
        block = _as_dict(payload.get(key))
        for ctx_key in ("billing_context", "pricing_context"):
            ctx = _as_dict(block.get(ctx_key))
            for field in ("billing_parent_job_id", "parent_longform_job_id", "parent_job_id"):
                value = _clean(ctx.get(field))
                if value:
                    return value
    return ""


class OrphanReconciledParentPricedSceneFusionExecutionService(
    ParentPricedSceneFusionExecutionService
):
    """Parent-priced scene execution with lost-create-response reconciliation.

    A provider child can be durably created even if the HTTP response is rejected by a
    downstream contract assertion. On a technical failed scene retry we discover those
    internal children by the parent stage/segment lineage persisted in studio_jobs.

    - succeeded child + concrete video URL -> adopt/reuse it
    - active child -> fail closed so a retry cannot create a duplicate provider render
    - failed/canceled child -> ignore it so normal failed-child pricing/retry can replace it

    This recovery is intentionally enabled only for stage_state=failed. User-requested
    revisions/rejections retain regenerate semantics.
    """

    async def _reconcile_orphan_children(
        self,
        conn,
        *,
        context,
        headers: dict[str, str],
    ) -> int:
        if _clean(context.stage_state).lower() != "failed":
            return 0

        latest = await _latest_attempt(conn, stage_run_id=context.stage_run_id)
        if not latest:
            return 0

        metadata = _as_dict(latest["metadata_json"])
        children = [dict(item or {}) for item in list(metadata.get("children") or [])]
        recorded_turns = {
            _clean(item.get("dialogue_turn_id"))
            for item in children
            if _clean(item.get("dialogue_turn_id"))
        }

        owner_user_id = await conn.fetchval(
            """
            select owner_user_id
            from public.v3_studio_workflows
            where workflow_id=$1 and account_id=$2
            """,
            context.workflow_id,
            context.account_id,
        )
        if not owner_user_id:
            return 0

        # Read only candidate V3 internal child jobs for this workflow owner. Exact
        # parent-stage/segment lineage is revalidated in Python before adoption.
        rows = await conn.fetch(
            """
            select id,status,payload_json,meta_json,created_at,updated_at
            from public.studio_jobs
            where studio_type='fusion'
              and user_id=$1
              and created_at >= ($2::timestamptz - interval '5 minutes')
            order by created_at desc
            limit 250
            """,
            owner_user_id,
            latest["started_at"],
        )

        by_turn: dict[str, Any] = {}
        parent_stage = str(context.stage_run_id)
        valid_turns = {str(turn.dialogue_turn_id): turn for turn in context.turns}

        for row in rows:
            payload = _as_dict(row["payload_json"])
            if _payload_parent_stage_id(payload) != parent_stage:
                continue
            turn_id = _payload_turn_id(payload)
            if not turn_id or turn_id not in valid_turns or turn_id in recorded_turns:
                continue
            # Rows are newest-first; one canonical candidate per dialogue turn.
            by_turn.setdefault(turn_id, row)

        adopted = 0
        for turn_id, row in by_turn.items():
            job_id = str(row["id"])
            persisted_state = _clean(row["status"]).lower()

            if persisted_state in _TERMINAL_FAILURE:
                continue

            try:
                status_payload = await self.fusion_client.status(
                    headers=headers,
                    job_id=job_id,
                )
            except Exception as exc:
                raise SceneFusionBridgeError(
                    f"fusion_existing_internal_child_status_unknown:{turn_id}:{job_id}:{str(exc)[:500]}"
                ) from exc

            state = _clean(status_payload.get("status") or persisted_state).lower()
            video_url = _video_url_from_status(status_payload)

            if state in _TERMINAL_SUCCESS and not video_url:
                status_full = getattr(self.fusion_client, "status_full", None)
                if callable(status_full):
                    try:
                        full = await status_full(headers=headers, job_id=job_id)
                        video_url = _video_url_from_status(full)
                    except Exception:
                        video_url = ""

            if state in _TERMINAL_FAILURE:
                continue

            if state not in _TERMINAL_SUCCESS or not video_url:
                # Never create a second external render while the first one may still
                # be in flight or finalizing.
                raise SceneFusionBridgeError(
                    f"fusion_existing_internal_child_still_running:{turn_id}:{job_id}:{state or 'unknown'}"
                )

            turn = valid_turns[turn_id]
            children.append(
                {
                    "dialogue_turn_id": turn_id,
                    "participant_id": str(getattr(turn, "participant_id", "") or ""),
                    "display_name": str(getattr(turn, "display_name", "") or ""),
                    "sequence_no": int(getattr(turn, "sequence_no", 0) or 0),
                    "fusion_job_id": job_id,
                    "status": "succeeded",
                    "video_url": video_url,
                    "pricing_suppressed": True,
                    "quote_id": None,
                    "preview_fingerprint": None,
                    "reused_from_prior_attempt": True,
                    "reconciled_orphan": True,
                }
            )
            recorded_turns.add(turn_id)
            adopted += 1

        if adopted:
            children.sort(key=lambda item: int(item.get("sequence_no") or 0))
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,
                    updated_at=now()
                where attempt_id=$1
                """,
                latest["attempt_id"],
                json.dumps(
                    {
                        "children": children,
                        "orphan_reconciled_count": adopted,
                        "orphan_reconciliation": "lost_create_response_recovered",
                    }
                ),
            )
        return adopted

    async def preview(
        self,
        conn,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        external_provider_ok: bool,
    ):
        context = await load_fusion_scene_context(
            conn,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
        )
        await self._reconcile_orphan_children(conn, context=context, headers=headers)
        return await super().preview(
            conn,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
            external_provider_ok=external_provider_ok,
        )

    async def dispatch(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        parent_confirmation: dict[str, Any],
        child_confirmations: list[dict[str, Any]],
        external_provider_ok: bool,
    ):
        async with pool.acquire() as conn:
            context = await load_fusion_scene_context(
                conn,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
            )
            await self._reconcile_orphan_children(conn, context=context, headers=headers)

        return await super().dispatch(
            pool,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
            parent_confirmation=parent_confirmation,
            child_confirmations=child_confirmations,
            external_provider_ok=external_provider_ok,
        )


__all__ = ["OrphanReconciledParentPricedSceneFusionExecutionService"]
