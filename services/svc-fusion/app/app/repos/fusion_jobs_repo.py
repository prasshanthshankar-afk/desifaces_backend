from __future__ import annotations

import json

from typing import Any, Dict, List, Optional
import asyncpg


class FusionJobsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def insert_job(self, user_id: int, request_hash: str, payload: Dict[str, Any]) -> str:
        """
        Recommended DB constraint for repeatable submit:
          UNIQUE (user_id, request_hash)

        With that in place, this becomes true idempotency:
          same request_hash => same job id returned.
        """
        sql = """
        INSERT INTO studio_jobs (studio_type, status, user_id, request_hash, payload_json, created_at, updated_at)
        VALUES ('fusion', 'queued', $1, $2, $3::jsonb, now(), now())
        ON CONFLICT (user_id, studio_type, request_hash)
        DO UPDATE SET updated_at = now()
        RETURNING id::text
        """
        async with self.pool.acquire() as conn:
            payload_json = json.dumps(payload, default=str)
            return await conn.fetchval(sql, user_id, request_hash, payload_json)

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
        SET status='running', updated_at=now()
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