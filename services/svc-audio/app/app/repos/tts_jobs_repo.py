from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import asyncpg


class TTSJobsRepo:
    """
    Queue access for audio studio_jobs.

    Pricing-safe lifecycle:
      - create pricing-enabled jobs as pricing_pending
      - move to queued only after reserve succeeds
      - worker only claims queued
    """

    def __init__(self, pool: asyncpg.Pool, *, studio_type: str = "audio"):
        self.pool = pool
        self.studio_type = studio_type

    async def insert_job(
        self,
        *,
        user_id: str,
        request_hash: str,
        payload: Dict[str, Any],
        initial_status: str = "queued",
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        sql = """
        INSERT INTO public.studio_jobs (
            studio_type,
            status,
            request_hash,
            payload_json,
            meta_json,
            user_id
        )
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::uuid)
        ON CONFLICT (user_id, studio_type, request_hash)
        DO UPDATE SET updated_at = now()
        RETURNING id::text
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                sql,
                self.studio_type,
                initial_status,
                request_hash,
                json.dumps(payload or {}, default=str),
                json.dumps(meta or {}, default=str),
                user_id,
            )

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

                await conn.execute(
                    """
                    UPDATE studio_jobs
                       SET status='running',
                           meta_json=COALESCE(meta_json, '{}'::jsonb)
                                     || jsonb_build_object('worker_claimed_at', now()::text),
                           updated_at=now()
                     WHERE id = ANY($1::uuid[])
                    """,
                    job_ids,
                )
                return job_ids

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  id::text AS id,
                  studio_type,
                  status,
                  user_id::text AS user_id,
                  request_hash,
                  payload_json,
                  meta_json,
                  error_code,
                  error_message,
                  attempt_count,
                  next_run_at,
                  created_at,
                  updated_at
                FROM public.studio_jobs
                WHERE id = $1::uuid
                """,
                job_id,
            )
        return dict(row) if row else None

    async def set_status(
        self,
        job_id: str,
        status: str,
        *,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.studio_jobs
                   SET status = $2,
                       error_code = COALESCE($3, error_code),
                       error_message = COALESCE($4, error_message),
                       updated_at = now()
                 WHERE id = $1::uuid
                """,
                job_id,
                status,
                error_code,
                error_message,
            )

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
                   AND status='running'
                """,
                job_id,
                int(delay_seconds),
                error_code,
                error_message,
            )
