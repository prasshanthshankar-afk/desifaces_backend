from __future__ import annotations

import json
from uuid import UUID

from df_contracts.v3.domain import JobState

from .face_execution import (
    ParticipantFaceExecutionService as BaseParticipantFaceExecutionService,
    _create_attempt,
    _definitive_dispatch_rejection,
    _latest_attempt,
    _update_attempt,
    compile_context_face_input,
    load_face_stage_context,
)
from .face_generation import CanonicalFaceGeneration
from .participant_face import ParticipantFaceBridgeError


class ParticipantFaceExecutionService(BaseParticipantFaceExecutionService):
    """Production Face slot execution with canonical C5 generation binding.

    The base class owns the proven svc-face dispatch/recovery/HITL mechanics.
    This wrapper creates the C5 request/root job before provider dispatch and
    reconciles that canonical job and output MediaAsset during every sync.
    """

    def __init__(self, *, face_base_url: str, store=None) -> None:
        super().__init__(face_base_url=face_base_url, store=store)
        self.canonical = CanonicalFaceGeneration()

    async def _mark_c5_failed(
        self,
        pool,
        *,
        attempt_id: UUID,
        generation_job_id: UUID,
        account_id: UUID,
        actor_user_id: UUID,
        error: Exception,
    ) -> None:
        async with pool.acquire() as conn:
            try:
                await self.canonical.transition(
                    conn,
                    generation_job_id=generation_job_id,
                    target=JobState.FAILED,
                    account_id=account_id,
                    actor_user_id=actor_user_id,
                    attempt_id=attempt_id,
                    error_code="face_dispatch_failed",
                    error_message=str(error),
                )
            except Exception:
                # Preserve the original dispatch failure. C5 reconciliation can be
                # retried independently and must never hide the Face API error.
                pass

    async def dispatch(
        self,
        pool,
        *,
        account_id: UUID,
        actor_user_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        quote_id: str,
        preview_fingerprint: str | None,
    ):
        """Create exactly one independently billable/retryable Face attempt.

        C5 request/root job and Studio attempt are persisted atomically before the
        external svc-face call. ``attempt_id`` is propagated as request_nonce, so
        ambiguous HTTP outcomes replay the same Face job rather than creating a
        second chargeable execution.
        """
        async with pool.acquire() as conn:
            async with conn.transaction():
                context = await load_face_stage_context(
                    conn,
                    account_id=account_id,
                    workflow_id=workflow_id,
                    stage_run_id=stage_run_id,
                    for_update=True,
                )
                prior_state = context.stage_state
                if prior_state not in {"pending", "ready", "failed", "rejected"}:
                    raise ParticipantFaceBridgeError(f"face_stage_not_dispatchable:{prior_state}")

                attempt_no = int(
                    await conn.fetchval(
                        "select coalesce(max(attempt_no),0)+1 from public.v3_studio_stage_attempts where stage_run_id=$1",
                        stage_run_id,
                    )
                )
                attempt_kind = "initial" if attempt_no == 1 else (
                    "regenerate" if prior_state == "rejected" else "retry"
                )
                attempt_id = await _create_attempt(
                    conn,
                    stage_run_id=stage_run_id,
                    attempt_no=attempt_no,
                    attempt_kind=attempt_kind,
                    quote_id=quote_id,
                    preview_fingerprint=preview_fingerprint,
                )
                workflow = await conn.fetchrow(
                    """select project_id,story_id from public.v3_studio_workflows
                    where workflow_id=$1 and account_id=$2""",
                    workflow_id,
                    account_id,
                )
                if not workflow:
                    raise ParticipantFaceBridgeError("face_workflow_not_found_or_account_mismatch")

                studio_input = compile_context_face_input(context)
                generation_id, generation_job_id, _ = await self.canonical.ensure(
                    conn,
                    account_id=account_id,
                    requested_by_user_id=actor_user_id,
                    project_id=UUID(str(workflow["project_id"])),
                    story_id=UUID(str(workflow["story_id"])) if workflow["story_id"] else None,
                    participant_id=context.participant_id,
                    stage_run_id=stage_run_id,
                    attempt_id=attempt_id,
                    attempt_no=attempt_no,
                    attempt_kind=attempt_kind,
                    studio_input=studio_input,
                    quote_id=quote_id,
                )
                await self.store.mark_generating(conn, stage_run_id=stage_run_id)
                await conn.execute(
                    """update public.v3_studio_stage_runs
                    set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where stage_run_id=$1""",
                    stage_run_id,
                    json.dumps({
                        "face_attempt_count": attempt_no,
                        "face_attempt_kind": attempt_kind,
                        "face_attempt_id": str(attempt_id),
                        "canonical_generation_id": str(generation_id),
                        "canonical_generation_job_id": str(generation_job_id),
                        "face_quote_id": quote_id,
                        "face_preview_fingerprint": preview_fingerprint,
                        "compatibility_face_job_id": None,
                        "dispatch_outcome": "dispatching",
                        "last_error": None,
                    }),
                )

        try:
            face_job_id = await self.face_client.create_job(
                headers=headers,
                studio_input=studio_input,
                pricing_preview={
                    "quote_id": quote_id,
                    "preview_fingerprint": preview_fingerprint,
                },
                request_nonce=str(attempt_id),
            )
        except Exception as exc:
            if _definitive_dispatch_rejection(exc):
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await _update_attempt(
                            conn,
                            attempt_id=attempt_id,
                            state="failed",
                            error_message=str(exc),
                        )
                        await self.store.mark_failed(
                            conn,
                            stage_run_id=stage_run_id,
                            error=str(exc),
                        )
                await self._mark_c5_failed(
                    pool,
                    attempt_id=attempt_id,
                    generation_job_id=generation_job_id,
                    account_id=account_id,
                    actor_user_id=actor_user_id,
                    error=exc,
                )
                raise

            await self._record_ambiguous_dispatch(
                pool,
                stage_run_id=stage_run_id,
                attempt_id=attempt_id,
                error=exc,
            )
            raise ParticipantFaceBridgeError(
                f"face_dispatch_outcome_unknown:{attempt_id}:{exc}"
            ) from exc

        try:
            await self._persist_job_correlation(
                pool,
                stage_run_id=stage_run_id,
                attempt_id=attempt_id,
                job_id=face_job_id,
            )
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self.canonical.set_compatibility_job(
                        conn,
                        generation_job_id=generation_job_id,
                        face_job_id=face_job_id,
                    )
                    await self.canonical.transition(
                        conn,
                        generation_job_id=generation_job_id,
                        target=JobState.QUEUED,
                        account_id=account_id,
                        actor_user_id=actor_user_id,
                        attempt_id=attempt_id,
                        metadata={"compatibility_face_job_id": face_job_id},
                    )
        except Exception as exc:
            await self._record_ambiguous_dispatch(
                pool,
                stage_run_id=stage_run_id,
                attempt_id=attempt_id,
                error=exc,
            )
            raise ParticipantFaceBridgeError(
                f"face_dispatch_correlation_unknown:{attempt_id}:{exc}"
            ) from exc

        return context, face_job_id, attempt_no, attempt_kind, attempt_id

    async def _reconcile_c5(
        self,
        pool,
        *,
        account_id: UUID,
        actor_user_id: UUID,
        stage_run_id: UUID,
        result: dict,
    ) -> None:
        async with pool.acquire() as conn:
            attempt = await _latest_attempt(conn, stage_run_id=stage_run_id)
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
                if current_state == JobState.SUBMITTED:
                    targets = (JobState.RUNNING, JobState.SUCCEEDED)
                elif current_state == JobState.QUEUED:
                    targets = (JobState.RUNNING, JobState.SUCCEEDED)
                elif current_state == JobState.RUNNING:
                    targets = (JobState.SUCCEEDED,)
                else:
                    targets = ()
            elif provider_state == "failed":
                targets = (JobState.FAILED,) if current_state not in {JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELED} else ()
            elif provider_state in {"cancelled", "canceled"}:
                targets = (JobState.CANCELED,) if current_state not in {JobState.CANCELED, JobState.SUCCEEDED, JobState.FAILED} else ()
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
                    error_message=str(result.get("error"))[:4000] if target == JobState.FAILED and result.get("error") else None,
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

    async def _adopt_compatibility_face_media(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> None:
        """
        Adopt a successfully generated svc-face compatibility asset into
        V3 canonical ownership.

        This method is intentionally strict:

        - it is invoked only after the normal Face reconciliation path
          rejected the asset because V3 account ownership was absent;
        - it never generates or regenerates media;
        - it proves the media belongs to the exact svc-face job correlated
          with this V3 stage attempt;
        - it never overwrites conflicting account/project/generation
          ownership;
        - it is safe to replay.
        """

        async with pool.acquire() as conn:
            correlation = await conn.fetchrow(
                """
                select
                    a.attempt_id,
                    a.provider_job_ref,
                    a.generation_job_id,
                    s.workflow_id,
                    w.account_id,
                    w.project_id
                from public.v3_studio_stage_attempts a
                join public.v3_studio_stage_runs s
                  on s.stage_run_id = a.stage_run_id
                join public.v3_studio_workflows w
                  on w.workflow_id = s.workflow_id
                where a.stage_run_id = $1
                order by a.attempt_no desc
                limit 1
                """,
                stage_run_id,
            )

        if not correlation:
            raise ParticipantFaceBridgeError(
                "face_media_adoption_missing_attempt"
            )

        if str(correlation["workflow_id"]) != str(workflow_id):
            raise ParticipantFaceBridgeError(
                "face_media_adoption_workflow_mismatch"
            )

        if str(correlation["account_id"]) != str(account_id):
            raise ParticipantFaceBridgeError(
                "face_media_adoption_account_mismatch"
            )

        generation_job_id = correlation["generation_job_id"]
        if not generation_job_id:
            raise ParticipantFaceBridgeError(
                "face_media_adoption_missing_generation_job"
            )

        face_job_id = str(
            correlation["provider_job_ref"] or ""
        ).strip()

        if not face_job_id:
            raise ParticipantFaceBridgeError(
                "face_media_adoption_missing_provider_job"
            )

        # Re-read the authoritative producer state.
        #
        # This is a status GET only. It cannot submit a new provider job.
        status_payload = await self._status_once(
            headers=headers,
            job_id=face_job_id,
        )

        provider_state = str(
            status_payload.get("status") or ""
        ).strip().lower()

        if provider_state != "succeeded":
            raise ParticipantFaceBridgeError(
                "face_media_adoption_provider_not_succeeded:"
                f"{provider_state}"
            )

        variants = list(status_payload.get("variants") or [])
        if not variants:
            raise ParticipantFaceBridgeError(
                f"face_succeeded_without_variants:{face_job_id}"
            )

        variant = dict(variants[0])

        raw_media_id = variant.get("media_asset_id")
        if not raw_media_id:
            raise ParticipantFaceBridgeError(
                "face_media_adoption_missing_media_id"
            )

        media_asset_id = UUID(str(raw_media_id))

        async with pool.acquire() as conn:
            async with conn.transaction():

                # Lock the media row so concurrent sync requests cannot
                # race ownership adoption.
                media = await conn.fetchrow(
                    """
                    select
                        id,
                        user_id,
                        account_id,
                        project_id,
                        parent_generation_job_id,
                        role
                    from public.media_assets
                    where id = $1
                    for update
                    """,
                    media_asset_id,
                )

                if not media:
                    raise ParticipantFaceBridgeError(
                        "face_media_adoption_asset_not_found"
                    )

                # Strong provenance check:
                # this exact media must be an output of this exact svc-face
                # provider job.
                linked_output = await conn.fetchval(
                    """
                    select exists(
                        select 1
                        from public.face_job_outputs
                        where job_id = $1::uuid
                          and output_asset_id = $2
                    )
                    """,
                    face_job_id,
                    media_asset_id,
                )

                if not linked_output:
                    raise ParticipantFaceBridgeError(
                        "face_media_adoption_provider_job_mismatch"
                    )

                # The producer job and media asset must belong to the
                # same originating user.
                producer_user_id = await conn.fetchval(
                    """
                    select user_id
                    from public.studio_jobs
                    where id = $1::uuid
                      and studio_type = 'face'
                    """,
                    face_job_id,
                )

                if not producer_user_id:
                    raise ParticipantFaceBridgeError(
                        "face_media_adoption_provider_job_not_found"
                    )

                if str(producer_user_id) != str(media["user_id"]):
                    raise ParticipantFaceBridgeError(
                        "face_media_adoption_user_mismatch"
                    )

                existing_account_id = media["account_id"]

                if (
                    existing_account_id is not None
                    and str(existing_account_id) != str(account_id)
                ):
                    # Never take ownership away from another account.
                    raise ParticipantFaceBridgeError(
                        "face_media_account_mismatch"
                    )

                workflow_project_id = correlation["project_id"]
                existing_project_id = media["project_id"]

                if (
                    existing_project_id is not None
                    and workflow_project_id is not None
                    and str(existing_project_id)
                    != str(workflow_project_id)
                ):
                    raise ParticipantFaceBridgeError(
                        "face_media_project_mismatch"
                    )

                existing_generation_job_id = media[
                    "parent_generation_job_id"
                ]

                if (
                    existing_generation_job_id is not None
                    and str(existing_generation_job_id)
                    != str(generation_job_id)
                ):
                    raise ParticipantFaceBridgeError(
                        "face_media_generation_job_mismatch"
                    )

                existing_role = str(media["role"] or "").strip()

                if existing_role and existing_role != "preview":
                    # An unapproved Face candidate must not silently
                    # inherit final/source semantics.
                    raise ParticipantFaceBridgeError(
                        "face_media_role_mismatch"
                    )

                # Idempotent adoption:
                # populate absent V3 context only.
                await conn.execute(
                    """
                    update public.media_assets
                    set
                        account_id =
                            coalesce(account_id, $2),
                        project_id =
                            coalesce(project_id, $3),
                        parent_generation_job_id =
                            coalesce(parent_generation_job_id, $4),
                        role =
                            coalesce(role, 'preview'),
                        updated_at = now()
                    where id = $1
                    """,
                    media_asset_id,
                    account_id,
                    workflow_project_id,
                    generation_job_id,
                )

    async def sync(
        self,
        pool,
        *,
        account_id: UUID,
        actor_user_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> dict:

        try:
            result = await super().sync(
                pool,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=headers,
            )

        except ParticipantFaceBridgeError as exc:

            # Do not mask unrelated Face errors.
            #
            # The compatibility repair is allowed ONLY for the exact
            # legacy-media ownership gap we have identified.
            if str(exc) != "face_media_account_mismatch":
                raise

            await self._adopt_compatibility_face_media(
                pool,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=headers,
            )

            # Replay reconciliation once.
            #
            # This calls the existing status API; it does NOT invoke
            # Face generation and does NOT reserve credits.
            result = await super().sync(
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
