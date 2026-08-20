from __future__ import annotations

from uuid import UUID

from df_contracts.v3.studio_workflow import StudioStageType, StudioWorkflowState, StudioWorkflowView
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore


async def _all_stages_approved(conn, *, workflow_id: UUID, stage_type: str) -> tuple[bool, int]:
    row = await conn.fetchrow(
        """
        select count(*) as total,
               count(*) filter(where state='approved') as approved
        from public.v3_studio_stage_runs
        where workflow_id=$1 and stage_type=$2
        """,
        workflow_id,
        stage_type,
    )
    total = int(row["total"] or 0)
    approved = int(row["approved"] or 0)
    return total > 0 and approved == total, total


async def _approved_active_output(conn, *, stage_run_id: UUID) -> UUID | None:
    value = await conn.fetchval(
        """
        select o.media_id
        from public.v3_studio_stage_outputs o
        join public.v3_studio_review_items r
          on r.stage_run_id=o.stage_run_id and r.media_id=o.media_id
        where o.stage_run_id=$1 and o.is_active=true and r.decision='approved'
        order by o.created_at desc limit 1
        """,
        stage_run_id,
    )
    return UUID(str(value)) if value else None


async def advance_studio_workflow(
    conn,
    *,
    store: CanonicalStudioWorkflowStore,
    workflow_id: UUID,
    account_id: UUID,
) -> StudioWorkflowView:
    """Advance only when the complete current-stage cohort has passed HITL."""
    view = await store.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)

    if view.current_stage == StudioStageType.FACE:
        face_cohort = next((item for item in view.cohorts if item.cohort_key == "face_cast"), None)
        if face_cohort and face_cohort.satisfied:
            await store.set_workflow_state(
                conn,
                workflow_id=workflow_id,
                state=StudioWorkflowState.ACTIVE,
                current_stage=StudioStageType.AUDIO,
            )
            return await store.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)
        return view

    if view.current_stage == StudioStageType.AUDIO:
        audio_ready, _ = await _all_stages_approved(
            conn, workflow_id=workflow_id, stage_type="audio"
        )
        if audio_ready:
            await store.set_workflow_state(
                conn,
                workflow_id=workflow_id,
                state=StudioWorkflowState.ACTIVE,
                current_stage=StudioStageType.FUSION,
            )
            return await store.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)
        return view

    if view.current_stage == StudioStageType.FUSION:
        fusion_ready, fusion_total = await _all_stages_approved(
            conn, workflow_id=workflow_id, stage_type="fusion"
        )
        if not fusion_ready:
            return view

        final_stage = await conn.fetchrow(
            """
            select stage_run_id,state from public.v3_studio_stage_runs
            where workflow_id=$1 and stage_type='story_final'
            order by created_at limit 1
            """,
            workflow_id,
        )
        if final_stage:
            await store.set_workflow_state(
                conn,
                workflow_id=workflow_id,
                state=StudioWorkflowState.ACTIVE,
                current_stage=StudioStageType.STORY_FINAL,
            )
            return await store.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)

        # A one-scene story has one logical Fusion stage and needs no extra assembly.
        if fusion_total == 1:
            fusion_stage_id = await conn.fetchval(
                "select stage_run_id from public.v3_studio_stage_runs where workflow_id=$1 and stage_type='fusion' limit 1",
                workflow_id,
            )
            final_media_id = await _approved_active_output(
                conn, stage_run_id=UUID(str(fusion_stage_id))
            )
            if final_media_id:
                await store.set_workflow_state(
                    conn,
                    workflow_id=workflow_id,
                    state=StudioWorkflowState.COMPLETED,
                    current_stage=StudioStageType.FUSION,
                    final_media_id=final_media_id,
                )
                return await store.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)
        return view

    if view.current_stage == StudioStageType.STORY_FINAL:
        final_ready, final_total = await _all_stages_approved(
            conn, workflow_id=workflow_id, stage_type="story_final"
        )
        if final_ready and final_total == 1:
            final_stage_id = await conn.fetchval(
                "select stage_run_id from public.v3_studio_stage_runs where workflow_id=$1 and stage_type='story_final' limit 1",
                workflow_id,
            )
            final_media_id = await _approved_active_output(
                conn, stage_run_id=UUID(str(final_stage_id))
            )
            if final_media_id:
                await store.set_workflow_state(
                    conn,
                    workflow_id=workflow_id,
                    state=StudioWorkflowState.COMPLETED,
                    current_stage=StudioStageType.STORY_FINAL,
                    final_media_id=final_media_id,
                )
                return await store.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)
        return view

    return view


__all__ = ["advance_studio_workflow"]
