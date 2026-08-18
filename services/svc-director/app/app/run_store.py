from __future__ import annotations

from typing import Any
from uuid import UUID


class DirectorRunNotFound(RuntimeError):
    pass


class DirectorRunStore:
    async def enqueue(self, conn, *, run_id: UUID, thread_id: str, account_id: UUID,
                      owner_user_id: UUID, brief: dict[str, Any]) -> None:
        await conn.execute(
            """insert into public.v3_director_runs(
              run_id,thread_id,account_id,owner_user_id,state,brief_json,available_at
            ) values($1,$2,$3,$4,'queued',$5::jsonb,now())""",
            run_id, thread_id, account_id, owner_user_id, brief,
        )

    async def get(self, conn, *, thread_id: str, account_id: UUID, owner_user_id: UUID):
        row = await conn.fetchrow(
            """select * from public.v3_director_runs
            where thread_id=$1 and account_id=$2 and owner_user_id=$3""",
            thread_id, account_id, owner_user_id,
        )
        if not row:
            raise DirectorRunNotFound(thread_id)
        return row

    async def queue_resume(self, conn, *, thread_id: str, account_id: UUID,
                           owner_user_id: UUID, resume_payload: dict[str, Any]):
        # A human revision is a new orchestration cycle, not a technical retry of
        # the previous cycle. Reset the technical attempt budget on each resume.
        row = await conn.fetchrow(
            """update public.v3_director_runs
            set state='queued',resume_json=$4::jsonb,attempt_count=0,available_at=now(),
                claimed_at=null,lease_expires_at=null,last_error=null,updated_at=now()
            where thread_id=$1 and account_id=$2 and owner_user_id=$3 and state='awaiting_review'
            returning *""",
            thread_id, account_id, owner_user_id, resume_payload,
        )
        if not row:
            raise DirectorRunNotFound(thread_id)
        return row

    async def claim_next(self, conn, *, lease_seconds: int = 900):
        return await conn.fetchrow(
            """with candidate as (
              select run_id from public.v3_director_runs
              where state='queued' and available_at<=now() and attempt_count<max_attempts
              order by available_at,created_at for update skip locked limit 1
            )
            update public.v3_director_runs r
            set state='running',attempt_count=r.attempt_count+1,claimed_at=now(),
                lease_expires_at=now()+($1::text || ' seconds')::interval,updated_at=now()
            from candidate c where r.run_id=c.run_id returning r.*""",
            int(lease_seconds),
        )

    async def recover_expired(self, conn) -> int:
        result = await conn.execute(
            """update public.v3_director_runs
            set state='queued',claimed_at=null,lease_expires_at=null,available_at=now(),updated_at=now()
            where state='running' and lease_expires_at is not null and lease_expires_at<now()
              and attempt_count<max_attempts"""
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    async def mark_awaiting_review(self, conn, *, run_id: UUID) -> None:
        await conn.execute(
            """update public.v3_director_runs
            set state='awaiting_review',resume_json=null,claimed_at=null,lease_expires_at=null,updated_at=now()
            where run_id=$1""",
            run_id,
        )

    async def mark_ready(self, conn, *, run_id: UUID, project_id: UUID | None,
                         story_id: UUID | None) -> None:
        await conn.execute(
            """update public.v3_director_runs
            set state='ready',project_id=$2,story_id=$3,resume_json=null,claimed_at=null,
                lease_expires_at=null,last_error=null,updated_at=now()
            where run_id=$1""",
            run_id, project_id, story_id,
        )

    async def mark_failed(self, conn, *, run_id: UUID, error: str) -> None:
        await conn.execute(
            """update public.v3_director_runs
            set state='failed',last_error=$2,resume_json=null,claimed_at=null,lease_expires_at=null,updated_at=now()
            where run_id=$1""",
            run_id, error[:4000],
        )
