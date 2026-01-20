from __future__ import annotations

from typing import List, Optional
import asyncpg


class TTSJobsRepo:
    """
    Queue access for audio studio_jobs.

    We claim jobs by:
      - selecting queued jobs ordered by next_run_at, created_at
      - locking rows with FOR UPDATE SKIP LOCKED
      - immediately flipping them to 'running'
    """

    def __init__(self, pool: asyncpg.Pool, *, studio_type: str = "audio"):
        self.pool = pool
        self.studio_type = studio_type

    async def fetch_next_queued_jobs(self, *, limit: int = 1) -> List[str]:
        limit = max(1, int(limit))

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT id::text
                    FROM studio_jobs
                    WHERE studio_type = $1
                      AND status = 'queued'
                      AND next_run_at <= now()
                    ORDER BY next_run_at ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT $2
                    """,
                    self.studio_type,
                    limit,
                )
                if not rows:
                    return []

                job_ids = [r["id"] for r in rows]

                # Claim: mark running so other workers won't pick it up.
                # NOTE: do NOT bump attempt_count here; orchestrator does it to avoid double-increment.
                await conn.execute(
                    """
                    UPDATE studio_jobs
                       SET status='running',
                           updated_at=now()
                     WHERE id = ANY($1::uuid[])
                    """,
                    job_ids,
                )
                return job_ids

    async def requeue_job(
        self,
        job_id: str,
        *,
        delay_seconds: int = 10,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE studio_jobs
                   SET status='queued',
                       next_run_at=now() + ($2::int * interval '1 second'),
                       updated_at=now(),
                       error_code=COALESCE($3, error_code),
                       error_message=COALESCE($4, error_message)
                 WHERE id=$1::uuid
                """,
                job_id,
                int(delay_seconds),
                error_code,
                error_message,
            )
