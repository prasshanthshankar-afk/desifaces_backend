from __future__ import annotations

from uuid import UUID

from df_contracts.v3.domain import JobState

from .face_execution import _latest_attempt
from .face_execution_canonical import ParticipantFaceExecutionService as CanonicalParticipantFaceExecutionService


class ParticipantFaceExecutionService(CanonicalParticipantFaceExecutionService):
    """Runtime composition for the existing Studio route contract.

    Existing routes predate explicit actor propagation into Face dispatch/sync.
    Until collaborative execution is introduced, the workflow owner is the
    canonical request actor for C5 generation attribution. The authenticated
    account boundary remains enforced by the route and workflow lookup.
    """

    async def _workflow_owner(self, pool, *, workflow_id: UUID, account_id: UUID) -> UUID:
        async with pool.acquire() as conn:
            owner = await conn.fetchval(
                "select owner_user_id from public.v3_studio_workflows where workflow_id=$1 and account_id=$2",
                workflow_id,
                account_id,
            )
        if not owner:
            raise RuntimeError("face_workflow_owner_not_found")
        return UUID(str(owner))

    async def dispatch(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        quote_id: str,
        preview_fingerprint: str | None,
    ):
        actor_user_id = await self._workflow_owner(
            pool,
            workflow_id=workflow_id,
            account_id=account_id,
        )
        return await super().dispatch(
            pool,
            account_id=account_id,
            actor_user_id=actor_user_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
            quote_id=quote_id,
            preview_fingerprint=preview_fingerprint,
        )

    async def _reconcile_c5(
        self,
        pool,
        *,
        account_id: UUID,
        actor_user_id: UUID,
        stage_run_id: UUID,
        result: dict,
    ) -> None:
        # Query the canonical IDs directly. The legacy helper intentionally
        # returns only compatibility-attempt fields for base sync semantics.
        async with pool.acquire() as conn:
            attempt = await conn.fetchrow(
                """select attempt_id,attempt_no,attempt_kind,state,provider_job_ref,media_id,
                          pricing_quote_id,preview_fingerprint,error_message,
                          generation_id,generation_job_id
                from public.v3_studio_stage_attempts
                where stage_run_id=$1 order by attempt_no desc limit 1""",
                stage_run_id,
            )
            if not attempt or not attempt["generation_job_id"]:
                return
            generation_job_id = UUID(str(attempt["generation_job_id"]))
            attempt_id = UUID(str(attempt["attempt_id"]))
            current = await conn.fetchval(
                "select state from public.v3_generation_jobs where job_id=$1",
                generation_job_id,
            )
            if not current:
                return
            current_state = JobState(str(current))

            provider_state = str(result.get("provider_state") or "").lower()
            if provider_state in {"pending", "queued", "dispatching"}:
                targets = (JobState.QUEUED,) if current_state == JobState.SUBMITTED else ()
            elif provider_state in {"running", "processing"}:
                targets = (JobState.RUNNING,) if current_state in {JobState.SUBMITTED, JobState.QUEUED} else ()
            elif provider_state == "succeeded":
                if current_state in {JobState.SUBMITTED, JobState.QUEUED}:
                    targets = (JobState.RUNNING, JobState.SUCCEEDED)
                elif current_state == JobState.RUNNING:
                    targets = (JobState.SUCCEEDED,)
                else:
                    targets = ()
            elif provider_state == "failed":
                targets = (JobState.FAILED,) if current_state not in {
                    JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELED,
                } else ()
            elif provider_state in {"cancelled", "canceled"}:
                targets = (JobState.CANCELED,) if current_state not in {
                    JobState.CANCELED, JobState.SUCCEEDED, JobState.FAILED,
                } else ()
            else:
                targets = ()

            for target in targets:
                await self.canonical.transition(
                    conn,
                    generation_job_id=generation_job_id,
                    target=target,
                    account_id=account_id,
                    actor_user_id=actor_user_id,
                    attempt_id=attempt_id,
                    error_code="face_generation_failed" if target == JobState.FAILED else None,
                    error_message=(
                        str(result.get("error"))[:4000]
                        if target == JobState.FAILED and result.get("error") else None
                    ),
                    metadata={
                        "provider_state": provider_state,
                        "compatibility_face_job_id": result.get("face_job_id"),
                    },
                )

            media_id = result.get("media_asset_id")
            if provider_state == "succeeded" and media_id:
                await self.canonical.attach_output(
                    conn,
                    account_id=account_id,
                    generation_job_id=generation_job_id,
                    media_id=UUID(str(media_id)),
                    stage_run_id=stage_run_id,
                    attempt_id=attempt_id,
                )

    async def sync(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> dict:
        actor_user_id = await self._workflow_owner(
            pool,
            workflow_id=workflow_id,
            account_id=account_id,
        )
        # Skip CanonicalParticipantFaceExecutionService.sync so we can supply
        # the corrected C5 attempt lookup exactly once after base Face sync.
        result = await super(CanonicalParticipantFaceExecutionService, self).sync(
            pool,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
        )
        await self._reconcile_c5(
            pool,
            account_id=account_id,
            actor_user_id=actor_user_id,
            stage_run_id=stage_run_id,
            result=result,
        )
        return result
