from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from df_contracts.v3.common import ActorType, RequestActor, RequestContext
from df_contracts.v3.domain import GenerationKind, GenerationRequest, JobState, SafetyState
from desifaces_shared.v3.generation_store import CanonicalGenerationStore
from desifaces_shared.v3.media_store import CanonicalMediaStore


def _canonical_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def generation_identity(attempt_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"desifaces:v3:studio-face-generation:{attempt_id}")


def request_context(*, account_id: UUID, actor_user_id: UUID, attempt_id: UUID) -> RequestContext:
    return RequestContext(
        request_id=attempt_id,
        correlation_id=attempt_id,
        actor=RequestActor(
            actor_type=ActorType.USER,
            actor_id=actor_user_id,
            account_id=account_id,
        ),
        idempotency_key=f"studio-face:{attempt_id}",
        client_app="svc-director",
    )


class CanonicalFaceGeneration:
    """C5 binding for one independently retryable Face output attempt.

    The Studio attempt is the retry/regenerate unit. C5 remains the canonical V3
    request/root-job/media audit. Pricing stays authoritative in svc-pricing and
    the actual AI-provider execution stays authoritative in svc-face.
    """

    def __init__(self) -> None:
        self.generations = CanonicalGenerationStore()
        self.media = CanonicalMediaStore()

    async def ensure(
        self,
        conn,
        *,
        account_id: UUID,
        requested_by_user_id: UUID,
        project_id: UUID,
        story_id: UUID | None,
        participant_id: UUID,
        stage_run_id: UUID,
        attempt_id: UUID,
        attempt_no: int,
        attempt_kind: str,
        studio_input: dict[str, Any],
        quote_id: str,
    ) -> tuple[UUID, UUID, RequestContext]:
        generation_id = generation_identity(attempt_id)
        ctx = request_context(
            account_id=account_id,
            actor_user_id=requested_by_user_id,
            attempt_id=attempt_id,
        )
        parameters = {
            "studio": "face",
            "stage_run_id": str(stage_run_id),
            "studio_attempt_id": str(attempt_id),
            "attempt_no": int(attempt_no),
            "attempt_kind": str(attempt_kind),
            "participant_id": str(participant_id),
            "story_id": str(story_id) if story_id else None,
            "pricing_quote_ref": str(quote_id),
            "studio_input_digest": _digest(studio_input),
        }
        request = GenerationRequest(
            generation_id=generation_id,
            account_id=account_id,
            requested_by_user_id=requested_by_user_id,
            project_id=project_id,
            story_id=story_id,
            kind=GenerationKind.FACE,
            participant_ids=(participant_id,),
            source_media_ids=(),
            parameters=parameters,
            pricing_quote_id=None,
            safety_state=SafetyState.PENDING,
            created_at=datetime.now(timezone.utc),
        )
        persistence = await self.generations.create_request_and_root_job(
            conn,
            request=request,
            context=ctx,
            idempotency_key=f"studio-face:{attempt_id}",
            request_digest=_digest(
                {
                    "generation_id": str(generation_id),
                    "participant_id": str(participant_id),
                    "stage_run_id": str(stage_run_id),
                    "attempt_kind": attempt_kind,
                    "studio_input": studio_input,
                }
            ),
            initial_state=JobState.SUBMITTED,
            compatibility_service="svc-face",
            compatibility_job_id=None,
            compatibility={
                "studio_workflow": True,
                "stage_run_id": str(stage_run_id),
                "studio_attempt_id": str(attempt_id),
            },
            metadata={
                "participant_id": str(participant_id),
                "attempt_no": int(attempt_no),
                "attempt_kind": str(attempt_kind),
            },
        )
        await conn.execute(
            """update public.v3_studio_stage_attempts
            set generation_id=$2,generation_job_id=$3,updated_at=now()
            where attempt_id=$1""",
            attempt_id,
            persistence.generation_id,
            persistence.job_id,
        )
        await conn.execute(
            """update public.v3_studio_stage_runs
            set generation_request_id=$2,generation_job_id=$3,updated_at=now()
            where stage_run_id=$1""",
            stage_run_id,
            persistence.generation_id,
            persistence.job_id,
        )
        return persistence.generation_id, persistence.job_id, ctx

    async def set_compatibility_job(
        self,
        conn,
        *,
        generation_job_id: UUID,
        face_job_id: str,
    ) -> None:
        await conn.execute(
            """update public.v3_generation_jobs
            set compatibility_service='svc-face',compatibility_job_id=$2,
                metadata_json=metadata_json || jsonb_build_object('compatibility_face_job_id',$2::text),
                updated_at=now()
            where job_id=$1""",
            generation_job_id,
            str(face_job_id),
        )

    async def transition(
        self,
        conn,
        *,
        generation_job_id: UUID,
        target: JobState,
        account_id: UUID,
        actor_user_id: UUID,
        attempt_id: UUID,
        error_code: str | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.generations.transition(
            conn,
            job_id=generation_job_id,
            target=target,
            context=request_context(
                account_id=account_id,
                actor_user_id=actor_user_id,
                attempt_id=attempt_id,
            ),
            error_code=error_code,
            error_message=error_message,
            metadata=metadata or {},
        )

    async def attach_output(
        self,
        conn,
        *,
        account_id: UUID,
        generation_job_id: UUID,
        media_id: UUID,
        stage_run_id: UUID,
        attempt_id: UUID,
    ) -> None:
        media = await self.media.get(conn, media_id=media_id, account_id=account_id)
        await self.generations.attach_media(
            conn,
            job_id=generation_job_id,
            media=media,
            relation="output",
            sequence_no=0,
            metadata={
                "studio_stage_run_id": str(stage_run_id),
                "studio_attempt_id": str(attempt_id),
                "output_role": "face_candidate",
            },
        )
