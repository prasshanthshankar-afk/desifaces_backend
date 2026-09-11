from __future__ import annotations

from typing import Any
from uuid import UUID


class DirectorRunNotFound(RuntimeError):
    pass


def _updated_rows(command_tag: str) -> int:
    try:
        return int(str(command_tag).split()[-1])
    except Exception:
        return 0


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

    async def list_recent(self, conn, *, account_id: UUID, owner_user_id: UUID, limit: int = 10):
        """Return the authenticated user's recent Stories with live Studio status.

        Story discovery is user-scoped rather than account-wide. Raw brief prose is
        never returned. The Director run state alone becomes stale after a Story
        enters Face/Audio/Fusion, so results are enriched from the canonical Studio
        workflow and stage states.
        """
        return await conn.fetch(
            """
            with latest_runs as (
              select distinct on (r.story_id)
                     r.run_id,r.thread_id,r.story_id,r.state,r.created_at,r.updated_at,
                     coalesce(nullif(r.brief_json->>'title',''),
                              nullif(r.brief_json->>'story_title',''),
                              nullif(r.brief_json->>'name','')) as title
                from public.v3_director_runs r
               where r.account_id=$1
                 and r.owner_user_id=$2
                 and r.story_id is not null
               order by r.story_id,coalesce(r.updated_at,r.created_at) desc,r.run_id desc
            )
            select r.run_id,r.thread_id,r.story_id,r.state,r.created_at,
                   greatest(
                     coalesce(r.updated_at,r.created_at),
                     coalesce(w.updated_at,r.created_at),
                     coalesce(w.stage_updated_at,r.created_at)
                   ) as updated_at,
                   r.title,
                   w.workflow_id,w.workflow_state,w.current_stage,
                   case
                     when w.workflow_id is null then r.state
                     when w.awaiting_review then 'awaiting_review'
                     when w.failed then 'failed'
                     when w.generating then 'generating'
                     when w.workflow_state in ('complete','completed') then 'complete'
                     else coalesce(w.workflow_state,r.state)
                   end as attention_state
              from latest_runs r
              left join lateral (
                select wf.workflow_id,
                       wf.state as workflow_state,
                       wf.current_stage,
                       wf.updated_at,
                       coalesce((select max(s.updated_at)
                                   from public.v3_studio_stage_runs s
                                  where s.workflow_id=wf.workflow_id),wf.updated_at) as stage_updated_at,
                       exists(select 1 from public.v3_studio_stage_runs s
                               where s.workflow_id=wf.workflow_id and s.state='awaiting_review') as awaiting_review,
                       exists(select 1 from public.v3_studio_stage_runs s
                               where s.workflow_id=wf.workflow_id and s.state='failed') as failed,
                       exists(select 1 from public.v3_studio_stage_runs s
                               where s.workflow_id=wf.workflow_id and s.state='generating') as generating
                  from public.v3_studio_workflows wf
                 where wf.story_id=r.story_id
                   and wf.account_id=$1
                   and wf.owner_user_id=$2
                 order by wf.updated_at desc,wf.workflow_id desc
                 limit 1
              ) w on true
             order by greatest(
                       coalesce(r.updated_at,r.created_at),
                       coalesce(w.updated_at,r.created_at),
                       coalesce(w.stage_updated_at,r.created_at)
                     ) desc
             limit $3
            """,
            account_id, owner_user_id, max(1, min(int(limit), 25)),
        )

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
                lease_expires_at=now()+make_interval(secs => $1::integer),updated_at=now()
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
        return _updated_rows(result)

    async def requeue_transient(
        self,
        conn,
        *,
        run_id: UUID,
        error: str,
        delay_seconds: int = 2,
    ) -> bool:
        """Release a claimed run after transient infrastructure failure.

        The technical attempt count remains monotonic. A run is requeued only while
        its configured attempt budget still has capacity; otherwise the caller may
        mark it failed. This prevents a temporary DNS/DB interruption from turning a
        durable queue item into a permanent user-visible failure after one attempt.
        """
        result = await conn.execute(
            """update public.v3_director_runs
            set state='queued',claimed_at=null,lease_expires_at=null,
                available_at=now()+make_interval(secs => $3::integer),
                last_error=$2,updated_at=now()
            where run_id=$1 and attempt_count<max_attempts""",
            run_id,
            str(error)[:4000],
            max(1, min(60, int(delay_seconds))),
        )
        return _updated_rows(result) == 1

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
