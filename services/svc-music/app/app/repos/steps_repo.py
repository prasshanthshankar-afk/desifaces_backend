from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from app.db import get_pool

try:
    import asyncpg  # type: ignore

    _IGNORABLE_EXC = (
        asyncpg.exceptions.UndefinedTableError,
        asyncpg.exceptions.ForeignKeyViolationError,
    )
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore
    _IGNORABLE_EXC = tuple()


class StepsRepo:
    async def upsert_step(
        self,
        *,
        job_id: UUID,
        step_code: str,
        status: str = "running",
        attempt: int = 0,
        error_code: str | None = None,
        error_message: str | None = None,
        meta_json: Dict[str, Any] | None = None,
    ) -> None:
        """
        Writes step status into public.studio_job_steps.

        IMPORTANT:
          - studio_job_steps.job_id has an FK to studio_jobs(id).
          - svc-music uses music_* job ids (e.g., music_video_jobs.id), which may NOT exist in studio_jobs.
          - To avoid FK violations + noisy Postgres ERROR logs, we only insert/update if studio_jobs row exists.
        """
        pool = await get_pool()
        try:
            await pool.execute(
                """
                insert into studio_job_steps(job_id, step_code, status, attempt, error_code, error_message, meta_json)
                select $1,$2,$3,$4,$5,$6,coalesce($7,'{}'::jsonb)
                where exists (select 1 from studio_jobs where id=$1)
                on conflict (job_id, step_code)
                do update set
                  status=excluded.status,
                  attempt=excluded.attempt,
                  error_code=excluded.error_code,
                  error_message=excluded.error_message,
                  meta_json=excluded.meta_json,
                  updated_at=now()
                """,
                job_id,
                step_code,
                status,
                attempt,
                error_code,
                error_message,
                meta_json or {},
            )
        except Exception as e:
            # Step logging must never break the job flow.
            # If the studio tables are absent or the FK still triggers in some edge case, ignore only known-safe cases.
            if _IGNORABLE_EXC and isinstance(e, _IGNORABLE_EXC):
                return
            # Unknown error: surface it (better than silently hiding a real bug)
            raise

    async def latest_step(self, *, job_id: UUID) -> Optional[dict]:
        pool = await get_pool()
        try:
            row = await pool.fetchrow(
                """
                select step_code, status, error_code, error_message, meta_json, updated_at
                from studio_job_steps
                where job_id=$1
                order by updated_at desc
                limit 1
                """,
                job_id,
            )
            return dict(row) if row else None
        except Exception as e:
            if _IGNORABLE_EXC and isinstance(e, (asyncpg.exceptions.UndefinedTableError,)):  # type: ignore
                return None
            raise