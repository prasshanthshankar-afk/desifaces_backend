"""Persistence boundary for the canonical Face -> Audio -> Fusion HITL workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID, uuid4

from df_contracts.v3.studio_workflow import (
    ReviewDecision,
    StudioArtifactRef,
    StudioReviewItem,
    StudioScopeType,
    StudioStageState,
    StudioStageType,
    StudioStageView,
    StudioWorkflowState,
    StudioWorkflowView,
)


class StudioWorkflowError(RuntimeError):
    pass


class StageDependencyNotApproved(StudioWorkflowError):
    pass


class StageReviewIncomplete(StudioWorkflowError):
    pass


class CanonicalStudioWorkflowStore:
    """Own durable studio handoffs and mandatory review gates.

    This store never calls Face, Audio or Fusion providers. Provider execution is
    represented by existing C5 GenerationRequest/GenerationJob identifiers.
    """

    async def create_workflow(
        self,
        conn,
        *,
        account_id: UUID,
        owner_user_id: UUID,
        project_id: UUID,
        story_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        workflow_id: UUID | None = None,
    ) -> UUID:
        workflow_id = workflow_id or uuid4()
        await conn.execute(
            """
            insert into public.v3_studio_workflows(
              workflow_id,account_id,owner_user_id,project_id,story_id,state,metadata_json
            ) values($1,$2,$3,$4,$5,'draft',$6::jsonb)
            """,
            workflow_id,
            account_id,
            owner_user_id,
            project_id,
            story_id,
            metadata or {},
        )
        return workflow_id

    async def add_stage(
        self,
        conn,
        *,
        workflow_id: UUID,
        stage_type: StudioStageType,
        scope_type: StudioScopeType,
        participant_id: UUID | None = None,
        scene_id: UUID | None = None,
        dialogue_turn_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        stage_run_id: UUID | None = None,
    ) -> UUID:
        stage_run_id = stage_run_id or uuid4()
        await conn.execute(
            """
            insert into public.v3_studio_stage_runs(
              stage_run_id,workflow_id,stage_type,scope_type,participant_id,scene_id,dialogue_turn_id,state,metadata_json
            ) values($1,$2,$3,$4,$5,$6,$7,'pending',$8::jsonb)
            """,
            stage_run_id,
            workflow_id,
            stage_type.value,
            scope_type.value,
            participant_id,
            scene_id,
            dialogue_turn_id,
            metadata or {},
        )
        return stage_run_id

    async def add_dependency(self, conn, *, parent_stage_run_id: UUID, child_stage_run_id: UUID) -> None:
        await conn.execute(
            """
            insert into public.v3_studio_stage_dependencies(parent_stage_run_id,child_stage_run_id)
            values($1,$2) on conflict do nothing
            """,
            parent_stage_run_id,
            child_stage_run_id,
        )

    async def bind_generation(
        self,
        conn,
        *,
        stage_run_id: UUID,
        generation_request_id: UUID,
        generation_job_id: UUID,
    ) -> None:
        await conn.execute(
            """
            update public.v3_studio_stage_runs
            set generation_request_id=$2,generation_job_id=$3,updated_at=now()
            where stage_run_id=$1
            """,
            stage_run_id,
            generation_request_id,
            generation_job_id,
        )

    async def assert_startable(self, conn, *, stage_run_id: UUID) -> None:
        blockers = await conn.fetchval(
            """
            select count(*)
            from public.v3_studio_stage_dependencies d
            join public.v3_studio_stage_runs p on p.stage_run_id=d.parent_stage_run_id
            where d.child_stage_run_id=$1 and p.state<>'approved'
            """,
            stage_run_id,
        )
        if int(blockers or 0):
            raise StageDependencyNotApproved(f"stage_dependencies_not_approved:{stage_run_id}")

    async def mark_generating(self, conn, *, stage_run_id: UUID) -> None:
        await self.assert_startable(conn, stage_run_id=stage_run_id)
        result = await conn.execute(
            """
            update public.v3_studio_stage_runs
            set state='generating',updated_at=now()
            where stage_run_id=$1 and state in ('pending','ready','rejected')
            """,
            stage_run_id,
        )
        if result.endswith("0"):
            raise StudioWorkflowError(f"stage_not_startable:{stage_run_id}")

    async def attach_output(
        self,
        conn,
        *,
        stage_run_id: UUID,
        media_id: UUID,
        output_role: str,
    ) -> UUID:
        await conn.execute(
            """
            insert into public.v3_studio_stage_outputs(stage_run_id,media_id,output_role)
            values($1,$2,$3) on conflict do nothing
            """,
            stage_run_id,
            media_id,
            output_role,
        )
        review_id = uuid4()
        await conn.execute(
            """
            insert into public.v3_studio_review_items(review_item_id,stage_run_id,media_id,decision)
            values($1,$2,$3,'pending')
            on conflict(stage_run_id,media_id) do nothing
            """,
            review_id,
            stage_run_id,
            media_id,
        )
        await conn.execute(
            "update public.v3_studio_stage_runs set state='awaiting_review',updated_at=now() where stage_run_id=$1",
            stage_run_id,
        )
        row = await conn.fetchrow(
            "select review_item_id from public.v3_studio_review_items where stage_run_id=$1 and media_id=$2",
            stage_run_id,
            media_id,
        )
        return UUID(str(row["review_item_id"]))

    async def review_output(
        self,
        conn,
        *,
        review_item_id: UUID,
        reviewer_user_id: UUID,
        decision: ReviewDecision,
        feedback: str | None = None,
    ) -> UUID:
        if decision == ReviewDecision.PENDING:
            raise StudioWorkflowError("review_decision_must_be_terminal")
        row = await conn.fetchrow(
            """
            update public.v3_studio_review_items
            set decision=$2,reviewer_user_id=$3,feedback=$4,decided_at=now(),updated_at=now()
            where review_item_id=$1
            returning stage_run_id
            """,
            review_item_id,
            decision.value,
            reviewer_user_id,
            feedback,
        )
        if not row:
            raise StudioWorkflowError(f"review_item_not_found:{review_item_id}")
        stage_run_id = UUID(str(row["stage_run_id"]))

        if decision == ReviewDecision.APPROVED:
            pending = await conn.fetchval(
                "select count(*) from public.v3_studio_review_items where stage_run_id=$1 and decision<>'approved'",
                stage_run_id,
            )
            if int(pending or 0) == 0:
                await conn.execute(
                    "update public.v3_studio_stage_runs set state='approved',updated_at=now() where stage_run_id=$1",
                    stage_run_id,
                )
        else:
            await conn.execute(
                "update public.v3_studio_stage_runs set state='rejected',updated_at=now() where stage_run_id=$1",
                stage_run_id,
            )
        return stage_run_id

    async def bind_approved_input(
        self,
        conn,
        *,
        stage_run_id: UUID,
        media_id: UUID,
        input_role: str,
        source_stage_run_id: UUID,
    ) -> None:
        await conn.execute(
            """
            insert into public.v3_studio_stage_inputs(stage_run_id,media_id,input_role,source_stage_run_id)
            values($1,$2,$3,$4) on conflict do nothing
            """,
            stage_run_id,
            media_id,
            input_role,
            source_stage_run_id,
        )

    async def set_workflow_state(
        self,
        conn,
        *,
        workflow_id: UUID,
        state: StudioWorkflowState,
        current_stage: StudioStageType | None = None,
        final_media_id: UUID | None = None,
    ) -> None:
        await conn.execute(
            """
            update public.v3_studio_workflows
            set state=$2,current_stage=$3,final_media_id=coalesce($4,final_media_id),updated_at=now()
            where workflow_id=$1
            """,
            workflow_id,
            state.value,
            current_stage.value if current_stage else None,
            final_media_id,
        )

    async def _stage_view(self, conn, row) -> StudioStageView:
        inputs = await conn.fetch(
            "select media_id,input_role,source_stage_run_id from public.v3_studio_stage_inputs where stage_run_id=$1 order by created_at,media_id",
            row["stage_run_id"],
        )
        outputs = await conn.fetch(
            "select media_id,output_role from public.v3_studio_stage_outputs where stage_run_id=$1 order by created_at,media_id",
            row["stage_run_id"],
        )
        reviews = await conn.fetch(
            """
            select review_item_id,stage_run_id,media_id,decision,reviewer_user_id,feedback,decided_at
            from public.v3_studio_review_items where stage_run_id=$1 order by created_at,review_item_id
            """,
            row["stage_run_id"],
        )
        return StudioStageView(
            stage_run_id=UUID(str(row["stage_run_id"])),
            workflow_id=UUID(str(row["workflow_id"])),
            stage_type=StudioStageType(str(row["stage_type"])),
            scope_type=StudioScopeType(str(row["scope_type"])),
            participant_id=UUID(str(row["participant_id"])) if row["participant_id"] else None,
            scene_id=UUID(str(row["scene_id"])) if row["scene_id"] else None,
            dialogue_turn_id=UUID(str(row["dialogue_turn_id"])) if row["dialogue_turn_id"] else None,
            state=StudioStageState(str(row["state"])),
            generation_request_id=UUID(str(row["generation_request_id"])) if row["generation_request_id"] else None,
            generation_job_id=UUID(str(row["generation_job_id"])) if row["generation_job_id"] else None,
            inputs=tuple(
                StudioArtifactRef(
                    media_id=UUID(str(item["media_id"])),
                    role=str(item["input_role"]),
                    source_stage_run_id=UUID(str(item["source_stage_run_id"])) if item["source_stage_run_id"] else None,
                ) for item in inputs
            ),
            outputs=tuple(
                StudioArtifactRef(media_id=UUID(str(item["media_id"])), role=str(item["output_role"]))
                for item in outputs
            ),
            reviews=tuple(
                StudioReviewItem(
                    review_item_id=UUID(str(item["review_item_id"])),
                    stage_run_id=UUID(str(item["stage_run_id"])),
                    media_id=UUID(str(item["media_id"])),
                    decision=ReviewDecision(str(item["decision"])),
                    reviewer_user_id=UUID(str(item["reviewer_user_id"])) if item["reviewer_user_id"] else None,
                    feedback=item["feedback"],
                    decided_at=item["decided_at"],
                ) for item in reviews
            ),
            metadata=dict(row["metadata_json"] or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_workflow(self, conn, *, workflow_id: UUID, account_id: UUID) -> StudioWorkflowView:
        workflow = await conn.fetchrow(
            """
            select workflow_id,account_id,owner_user_id,project_id,story_id,state,current_stage,final_media_id,created_at,updated_at
            from public.v3_studio_workflows where workflow_id=$1 and account_id=$2
            """,
            workflow_id,
            account_id,
        )
        if not workflow:
            raise StudioWorkflowError(f"workflow_not_found:{workflow_id}")
        rows = await conn.fetch(
            "select * from public.v3_studio_stage_runs where workflow_id=$1 order by created_at,stage_run_id",
            workflow_id,
        )
        stages = tuple([await self._stage_view(conn, row) for row in rows])
        pending_review = any(stage.state == StudioStageState.AWAITING_REVIEW for stage in stages)
        next_action = "review_stage_output" if pending_review else None
        return StudioWorkflowView(
            workflow_id=UUID(str(workflow["workflow_id"])),
            account_id=UUID(str(workflow["account_id"])),
            owner_user_id=UUID(str(workflow["owner_user_id"])),
            project_id=UUID(str(workflow["project_id"])),
            story_id=UUID(str(workflow["story_id"])) if workflow["story_id"] else None,
            state=StudioWorkflowState(str(workflow["state"])),
            current_stage=StudioStageType(str(workflow["current_stage"])) if workflow["current_stage"] else None,
            stages=stages,
            final_media_id=UUID(str(workflow["final_media_id"])) if workflow["final_media_id"] else None,
            next_action=next_action,
            created_at=workflow["created_at"],
            updated_at=workflow["updated_at"],
        )
