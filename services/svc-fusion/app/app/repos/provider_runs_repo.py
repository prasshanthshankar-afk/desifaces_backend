from __future__ import annotations

import json
from typing import Any, Dict, Optional
import asyncpg


class ProviderRunsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_by_idempotency_key(self, idempotency_key: str) -> Optional[asyncpg.Record]:
        q = "SELECT * FROM provider_runs WHERE idempotency_key = $1"
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(q, idempotency_key)

    async def create_run(
        self,
        job_id: str,
        provider: str,
        idempotency_key: str,
        request_json: Dict[str, Any],
    ) -> str:
        q = """
        INSERT INTO provider_runs (job_id, provider, idempotency_key, provider_status, request_json)
        VALUES ($1::uuid, $2, $3, 'created', $4::jsonb)
        RETURNING id::text
        """
        request_json_str = json.dumps(request_json)
        async with self.pool.acquire() as conn:
            return await conn.fetchval(q, job_id, provider, idempotency_key, request_json_str)

    async def mark_submitted(self, run_id: str, provider_job_id: str, response_json: Dict[str, Any]) -> None:
        q = """
        UPDATE provider_runs
        SET provider_job_id=$2,
            provider_status='submitted',
            response_json=$3::jsonb,
            updated_at=now()
        WHERE id=$1::uuid
        """
        response_json_str = json.dumps(response_json)
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, provider_job_id, response_json_str)

    async def update_status(self, run_id: str, status: str, meta_json: Optional[Dict[str, Any]] = None) -> None:
        q = """
        UPDATE provider_runs
        SET provider_status=$2,
            meta_json=COALESCE($3::jsonb, meta_json),
            updated_at=now()
        WHERE id=$1::uuid
        """
        meta_json_str = json.dumps(meta_json) if meta_json is not None else None
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, status, meta_json_str)