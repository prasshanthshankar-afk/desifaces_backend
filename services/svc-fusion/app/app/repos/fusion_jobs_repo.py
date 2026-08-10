from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import asyncpg


class FusionJobsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def insert_job(
        self,
        user_id: str,
        request_hash: str,
        payload: Dict[str, Any],
        initial_status: str = "queued",
    ) -> str:
        """
        Recommended DB constraint for repeatable submit:
          UNIQUE (user_id, studio_type, request_hash)

        Important:
          - Create pricing-enabled jobs as pricing_pending first.
          - Only move to queued after reserve succeeds.
          - Worker claims queued only, so pricing_pending jobs can never run.
        """
        sql = """
        INSERT INTO studio_jobs (
            studio_type,
            status,
            user_id,
            request_hash,
            payload_json,
            created_at,
            updated_at
        )
        VALUES ('fusion', $4, $1, $2, $3::jsonb, now(), now())
        ON CONFLICT (user_id, studio_type, request_hash)
        DO UPDATE SET updated_at = now()
        RETURNING id::text
        """
        async with self.pool.acquire() as conn:
            payload_json = json.dumps(payload, default=str)
            return await conn.fetchval(sql, user_id, request_hash, payload_json, initial_status)

    async def claim_next_jobs(self, studio_type: str, limit: int = 1) -> List[str]:
        sql = """
        WITH cte AS (
            SELECT id
            FROM studio_jobs
            WHERE studio_type = $1
              AND status = 'queued'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        UPDATE studio_jobs j
        SET status = 'running',
            updated_at = now()
        FROM cte
        WHERE j.id = cte.id
        RETURNING j.id::text;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, studio_type, limit)
        return [str(r["id"]) for r in rows]

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        sql = """
        SELECT
          id,
          studio_type,
          status,
          user_id,
          request_hash,
          payload_json,
          meta_json,
          error_code,
          error_message,
          created_at,
          updated_at
        FROM studio_jobs
        WHERE id = $1::uuid
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, job_id)
        return dict(row) if row else None

    async def set_status(
        self,
        job_id: str,
        status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        sql = """
        UPDATE studio_jobs
        SET status = $2,
            error_code = COALESCE($3, error_code),
            error_message = COALESCE($4, error_message),
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self.pool.acquire() as conn:
            await conn.execute(sql, job_id, status, error_code, error_message)

    async def claim_stale_processing_jobs(
        self,
        studio_type: str,
        *,
        limit: int,
        stale_seconds: int,
        claim_ttl_seconds: int,
        owner: str,
    ) -> List[str]:
        sql = """
        WITH cand AS (
            SELECT id
            FROM studio_jobs
            WHERE studio_type = $1
              AND status = 'processing'
              AND updated_at < now() - make_interval(secs => $2::int)
              AND COALESCE(NULLIF(meta_json->>'recovery_claimed_at', '')::timestamptz, to_timestamp(0))
                    < now() - make_interval(secs => $3::int)
            ORDER BY updated_at
            FOR UPDATE SKIP LOCKED
            LIMIT $4
        )
        UPDATE studio_jobs j
        SET meta_json = COALESCE(j.meta_json, '{}'::jsonb)
                        || jsonb_build_object(
                             'recovery_claimed_at', now()::text,
                             'recovery_owner', $5::text,
                             'recovery_reason', 'stale_processing',
                             'recovery_attempts', COALESCE((NULLIF(j.meta_json->>'recovery_attempts', ''))::int, 0) + 1
                           ),
            updated_at = now()
        FROM cand
        WHERE j.id = cand.id
        RETURNING j.id::text
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, studio_type, stale_seconds, claim_ttl_seconds, limit, owner)
        return [str(r['id']) for r in rows]
