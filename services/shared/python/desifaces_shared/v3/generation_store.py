from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from df_contracts.v3.common import RequestContext
from df_contracts.v3.domain import (
    GenerationJob,
    GenerationRequest,
    JobState,
    MediaAsset,
    ProviderExecution,
    ProviderExecutionState,
)


class InvalidJobTransition(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.SUBMITTED: frozenset({JobState.QUEUED, JobState.RUNNING, JobState.BLOCKED, JobState.CANCELED, JobState.EXPIRED, JobState.FAILED}),
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.BLOCKED, JobState.CANCELED, JobState.EXPIRED, JobState.FAILED}),
    JobState.RUNNING: frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.BLOCKED, JobState.CANCELED, JobState.EXPIRED, JobState.QUEUED}),
    JobState.BLOCKED: frozenset({JobState.QUEUED, JobState.CANCELED, JobState.EXPIRED, JobState.FAILED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELED: frozenset(),
    JobState.EXPIRED: frozenset(),
}


def validate_job_transition(current: JobState | str, target: JobState | str) -> tuple[JobState, JobState]:
    old = current if isinstance(current, JobState) else JobState(str(current))
    new = target if isinstance(target, JobState) else JobState(str(target))
    if old == new:
        return old, new
    if new not in _ALLOWED_TRANSITIONS[old]:
        raise InvalidJobTransition(f"invalid_generation_job_transition:{old.value}->{new.value}")
    return old, new


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        pass
    try:
        value = row.get(key)
        return default if value is None else value
    except Exception:
        return default


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


@dataclass(frozen=True)
class GenerationPersistenceResult:
    generation_id: UUID
    job_id: UUID
    created: bool


class CanonicalGenerationStore:
    """Canonical V3 generation request/job persistence boundary.

    It is provider-agnostic and does not invoke AI providers, pricing, queues or
    storage. New V3 orchestration composes those concerns around this store.
    """

    async def create_request_and_root_job(
        self,
        conn,
        *,
        request: GenerationRequest,
        context: RequestContext,
        idempotency_key: str,
        request_digest: str,
        initial_state: JobState = JobState.SUBMITTED,
        compatibility_service: str | None = None,
        compatibility_job_id: str | None = None,
        compatibility: Optional[Mapping[str, Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> GenerationPersistenceResult:
        key = str(idempotency_key or "").strip()
        digest = str(request_digest or "").strip()
        if not key:
            raise ValueError("generation_idempotency_key_required")
        if not digest:
            raise ValueError("generation_request_digest_required")

        existing = await conn.fetchrow(
            """
            select generation_id, request_digest
            from public.v3_generation_requests
            where account_id=$1 and idempotency_key=$2
            for update
            """,
            request.account_id,
            key,
        )
        if existing:
            if str(_row_get(existing, "request_digest")) != digest:
                raise IdempotencyConflict(f"generation_idempotency_conflict:{key}")
            generation_id = UUID(str(_row_get(existing, "generation_id")))
            job = await conn.fetchrow(
                """
                select job_id from public.v3_generation_jobs
                where generation_id=$1 and parent_job_id is null and job_type='root'
                limit 1
                """,
                generation_id,
            )
            if not job:
                raise RuntimeError(f"canonical_root_job_missing:{generation_id}")
            return GenerationPersistenceResult(
                generation_id=generation_id,
                job_id=UUID(str(_row_get(job, "job_id"))),
                created=False,
            )

        generation_id = request.generation_id
        await conn.execute(
            """
            insert into public.v3_generation_requests(
              generation_id, account_id, requested_by_user_id, project_id,
              generation_kind, participant_ids, source_media_ids, parameters_json,
              pricing_quote_id, safety_state, idempotency_key, request_digest,
              request_context_json, compatibility_json, created_at
            ) values(
              $1,$2,$3,$4,$5,$6::uuid[],$7::uuid[],$8::jsonb,$9,$10,$11,$12,$13::jsonb,$14::jsonb,$15
            )
            """,
            generation_id,
            request.account_id,
            request.requested_by_user_id,
            request.project_id,
            request.kind.value,
            list(request.participant_ids),
            list(request.source_media_ids),
            _json(request.parameters),
            request.pricing_quote_id,
            request.safety_state.value,
            key,
            digest,
            _json(context.model_dump(mode="json")),
            _json(dict(compatibility or {})),
            request.created_at,
        )

        job_id = uuid4()
        now = datetime.now(timezone.utc)
        await conn.execute(
            """
            insert into public.v3_generation_jobs(
              job_id, generation_id, parent_job_id, job_type, state,
              compatibility_service, compatibility_job_id, metadata_json,
              created_at, updated_at
            ) values($1,$2,null,'root',$3,$4,$5,$6::jsonb,$7,$7)
            """,
            job_id,
            generation_id,
            initial_state.value,
            compatibility_service,
            compatibility_job_id,
            _json(dict(metadata or {})),
            now,
        )
        await self._append_event(
            conn,
            job_id=job_id,
            from_state=None,
            to_state=initial_state,
            context=context,
            event_type="created",
            metadata={"generation_kind": request.kind.value},
        )
        return GenerationPersistenceResult(generation_id=generation_id, job_id=job_id, created=True)

    async def create_child_job(
        self,
        conn,
        *,
        generation_id: UUID,
        parent_job_id: UUID,
        job_type: str,
        context: RequestContext,
        state: JobState = JobState.SUBMITTED,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> UUID:
        parent = await conn.fetchrow(
            "select generation_id from public.v3_generation_jobs where job_id=$1",
            parent_job_id,
        )
        if not parent or str(_row_get(parent, "generation_id")) != str(generation_id):
            raise ValueError("parent_job_generation_mismatch")
        job_id = uuid4()
        now = datetime.now(timezone.utc)
        await conn.execute(
            """
            insert into public.v3_generation_jobs(
              job_id,generation_id,parent_job_id,job_type,state,metadata_json,created_at,updated_at
            ) values($1,$2,$3,$4,$5,$6::jsonb,$7,$7)
            """,
            job_id,
            generation_id,
            parent_job_id,
            str(job_type or "child"),
            state.value,
            _json(dict(metadata or {})),
            now,
        )
        await self._append_event(
            conn,
            job_id=job_id,
            from_state=None,
            to_state=state,
            context=context,
            event_type="created_child",
            metadata={"parent_job_id": str(parent_job_id), "job_type": job_type},
        )
        return job_id

    async def transition(
        self,
        conn,
        *,
        job_id: UUID,
        target: JobState,
        context: RequestContext,
        progress_percent: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> GenerationJob:
        row = await conn.fetchrow(
            "select state from public.v3_generation_jobs where job_id=$1 for update",
            job_id,
        )
        if not row:
            raise RuntimeError(f"generation_job_not_found:{job_id}")
        old, new = validate_job_transition(str(_row_get(row, "state")), target)

        await conn.execute(
            """
            update public.v3_generation_jobs
            set state=$2,
                progress_percent=coalesce($3,progress_percent),
                error_code=$4,
                error_message=$5,
                metadata_json=metadata_json || $6::jsonb,
                heartbeat_at=case when $2='running' then now() else heartbeat_at end,
                updated_at=now()
            where job_id=$1
            """,
            job_id,
            new.value,
            progress_percent,
            error_code,
            error_message,
            _json(dict(metadata or {})),
        )
        await self._append_event(
            conn,
            job_id=job_id,
            from_state=old,
            to_state=new,
            context=context,
            metadata={
                **dict(metadata or {}),
                "progress_percent": progress_percent,
                "error_code": error_code,
            },
        )
        return await self.get_job(conn, job_id=job_id)

    async def register_provider_execution(
        self,
        conn,
        *,
        job_id: UUID,
        provider: str,
        capability: str,
        model: str | None = None,
        attempt: int = 1,
        idempotency_key: str | None = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ProviderExecution:
        row = await conn.fetchrow(
            """
            insert into public.v3_provider_executions(
              execution_id,job_id,provider,capability,model,state,attempt,idempotency_key,metadata_json,created_at,updated_at
            ) values(gen_random_uuid(),$1,$2,$3,$4,'planned',$5,$6,$7::jsonb,now(),now())
            on conflict(job_id,provider,capability,attempt)
            do update set metadata_json=public.v3_provider_executions.metadata_json || excluded.metadata_json,
                          idempotency_key=coalesce(public.v3_provider_executions.idempotency_key,excluded.idempotency_key),
                          updated_at=now()
            returning *
            """,
            job_id,
            str(provider),
            str(capability),
            model,
            max(1, int(attempt)),
            idempotency_key,
            _json(dict(metadata or {})),
        )
        return self._provider_from_row(row)

    async def update_provider_execution(
        self,
        conn,
        *,
        execution_id: UUID,
        state: ProviderExecutionState,
        provider_request_id: str | None = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ProviderExecution:
        row = await conn.fetchrow(
            """
            update public.v3_provider_executions
            set state=$2,
                provider_request_id=coalesce($3,provider_request_id),
                metadata_json=metadata_json || $4::jsonb,
                started_at=case when $2 in ('submitted','running') then coalesce(started_at,now()) else started_at end,
                completed_at=case when $2 in ('succeeded','failed','canceled') then coalesce(completed_at,now()) else completed_at end,
                updated_at=now()
            where execution_id=$1
            returning *
            """,
            execution_id,
            state.value,
            provider_request_id,
            _json(dict(metadata or {})),
        )
        if not row:
            raise RuntimeError(f"provider_execution_not_found:{execution_id}")
        return self._provider_from_row(row)

    async def attach_media(
        self,
        conn,
        *,
        job_id: UUID,
        media: MediaAsset,
        relation: str,
        sequence_no: int = 0,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if relation not in {"input", "intermediate", "preview", "output", "thumbnail"}:
            raise ValueError(f"invalid_generation_media_relation:{relation}")
        account = await conn.fetchrow(
            """
            select r.account_id
            from public.v3_generation_jobs j
            join public.v3_generation_requests r on r.generation_id=j.generation_id
            where j.job_id=$1
            """,
            job_id,
        )
        if not account:
            raise RuntimeError(f"generation_job_not_found:{job_id}")
        if str(_row_get(account, "account_id")) != str(media.account_id):
            raise RuntimeError("generation_media_account_mismatch")

        await conn.execute(
            """
            insert into public.v3_generation_job_media(job_id,media_id,relation,sequence_no,metadata_json)
            values($1,$2,$3,$4,$5::jsonb)
            on conflict(job_id,media_id,relation)
            do update set sequence_no=excluded.sequence_no,
                          metadata_json=public.v3_generation_job_media.metadata_json || excluded.metadata_json
            """,
            job_id,
            media.media_id,
            relation,
            max(0, int(sequence_no)),
            _json(dict(metadata or {})),
        )
        if relation in {"intermediate", "preview", "output", "thumbnail"}:
            await conn.execute(
                """
                update public.media_assets
                set parent_generation_job_id=coalesce(parent_generation_job_id,$2), updated_at=now()
                where id=$1
                """,
                media.media_id,
                job_id,
            )

    async def claim_next(
        self,
        conn,
        *,
        lease_owner: str,
        lease_seconds: int = 120,
        generation_kinds: Sequence[str] = (),
    ) -> UUID | None:
        """Atomically claim one runnable job without exceeding retry budget."""
        row = await conn.fetchrow(
            """
            with candidate as (
              select j.job_id
              from public.v3_generation_jobs j
              join public.v3_generation_requests r on r.generation_id=j.generation_id
              where j.state in ('submitted','queued')
                and j.available_at <= now()
                and j.attempt_count < j.max_attempts
                and (coalesce(array_length($1::text[],1),0)=0 or r.generation_kind = any($1::text[]))
                and (j.lease_expires_at is null or j.lease_expires_at <= now())
              order by j.available_at asc, j.created_at asc
              for update of j skip locked
              limit 1
            )
            update public.v3_generation_jobs j
            set state='running',
                attempt_count=attempt_count+1,
                claimed_at=now(),
                heartbeat_at=now(),
                lease_owner=$2,
                lease_expires_at=now()+make_interval(secs=>$3),
                updated_at=now()
            from candidate c
            where j.job_id=c.job_id
            returning j.job_id
            """,
            list(generation_kinds),
            str(lease_owner),
            max(30, int(lease_seconds)),
        )
        return UUID(str(_row_get(row, "job_id"))) if row else None

    async def heartbeat(self, conn, *, job_id: UUID, lease_owner: str, lease_seconds: int = 120) -> bool:
        status = await conn.execute(
            """
            update public.v3_generation_jobs
            set heartbeat_at=now(), lease_expires_at=now()+make_interval(secs=>$3), updated_at=now()
            where job_id=$1 and state='running' and lease_owner=$2
            """,
            job_id,
            str(lease_owner),
            max(30, int(lease_seconds)),
        )
        return not str(status).endswith("0")

    async def get_job(self, conn, *, job_id: UUID) -> GenerationJob:
        row = await conn.fetchrow(
            "select * from public.v3_generation_jobs where job_id=$1",
            job_id,
        )
        if not row:
            raise RuntimeError(f"generation_job_not_found:{job_id}")
        provider_rows = await conn.fetch(
            "select execution_id from public.v3_provider_executions where job_id=$1 order by created_at,execution_id",
            job_id,
        )
        output_rows = await conn.fetch(
            "select media_id from public.v3_generation_job_media where job_id=$1 and relation='output' order by sequence_no,created_at",
            job_id,
        )
        return GenerationJob(
            job_id=UUID(str(_row_get(row, "job_id"))),
            generation_id=UUID(str(_row_get(row, "generation_id"))),
            parent_job_id=UUID(str(_row_get(row, "parent_job_id"))) if _row_get(row, "parent_job_id") else None,
            job_type=str(_row_get(row, "job_type", "root")),
            state=JobState(str(_row_get(row, "state"))),
            progress_percent=_row_get(row, "progress_percent"),
            attempt_count=int(_row_get(row, "attempt_count", 0)),
            max_attempts=int(_row_get(row, "max_attempts", 3)),
            provider_execution_ids=tuple(UUID(str(_row_get(item, "execution_id"))) for item in provider_rows),
            output_media_ids=tuple(UUID(str(_row_get(item, "media_id"))) for item in output_rows),
            error_code=_row_get(row, "error_code"),
            error_message=_row_get(row, "error_message"),
            created_at=_row_get(row, "created_at"),
            updated_at=_row_get(row, "updated_at"),
        )

    async def _append_event(
        self,
        conn,
        *,
        job_id: UUID,
        from_state: JobState | None,
        to_state: JobState,
        context: RequestContext,
        event_type: str = "state_transition",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        await conn.execute(
            """
            insert into public.v3_generation_job_events(
              job_id,from_state,to_state,event_type,actor_type,actor_id,request_id,metadata_json
            ) values($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
            """,
            job_id,
            from_state.value if from_state else None,
            to_state.value,
            event_type,
            context.actor.actor_type.value,
            str(context.actor.actor_id),
            str(context.request_id),
            _json(dict(metadata or {})),
        )

    @staticmethod
    def _provider_from_row(row: Any) -> ProviderExecution:
        if not row:
            raise RuntimeError("provider_execution_missing")
        return ProviderExecution(
            execution_id=UUID(str(_row_get(row, "execution_id"))),
            job_id=UUID(str(_row_get(row, "job_id"))),
            provider=str(_row_get(row, "provider")),
            capability=str(_row_get(row, "capability")),
            model=_row_get(row, "model"),
            state=ProviderExecutionState(str(_row_get(row, "state"))),
            provider_request_id=_row_get(row, "provider_request_id"),
            idempotency_key=_row_get(row, "idempotency_key"),
            attempt=int(_row_get(row, "attempt", 1)),
            metadata=_as_dict(_row_get(row, "metadata_json", {})),
            started_at=_row_get(row, "started_at"),
            completed_at=_row_get(row, "completed_at"),
        )
