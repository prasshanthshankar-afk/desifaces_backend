from __future__ import annotations
import json
from typing import Any, Dict, Optional
import asyncpg


class StepsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def upsert_step(self, job_id: str, step_code: str, status: str, attempt: int = 0, meta_json: Dict[str, Any] | None = None) -> None:
        """
        Upsert a step - check if exists, update if so, insert if not.
        Simpler approach without ON CONFLICT.
        """
        meta_json_str = json.dumps(meta_json if meta_json is not None else {})
        
        async with self.pool.acquire() as conn:
            # Check if step exists
            exists = await conn.fetchval(
                "SELECT 1 FROM studio_job_steps WHERE job_id = $1::uuid AND step_code = $2",
                job_id, step_code
            )
            
            if exists:
                # Update existing step
                await conn.execute(
                    """
                    UPDATE studio_job_steps 
                    SET status = $3, attempt = $4, meta_json = $5::jsonb, updated_at = now()
                    WHERE job_id = $1::uuid AND step_code = $2
                    """,
                    job_id, step_code, status, attempt, meta_json_str
                )
            else:
                # Insert new step
                await conn.execute(
                    """
                    INSERT INTO studio_job_steps (job_id, step_code, status, attempt, meta_json)
                    VALUES ($1::uuid, $2, $3, $4, $5::jsonb)
                    """,
                    job_id, step_code, status, attempt, meta_json_str
                )

    async def fail_step(self, job_id: str, step_code: str, attempt: int, error_code: str, error_message: str) -> None:
        """
        Mark a step as failed.
        """
        async with self.pool.acquire() as conn:
            # Check if step exists
            exists = await conn.fetchval(
                "SELECT 1 FROM studio_job_steps WHERE job_id = $1::uuid AND step_code = $2",
                job_id, step_code
            )
            
            if exists:
                # Update existing
                await conn.execute(
                    """
                    UPDATE studio_job_steps 
                    SET status = 'failed', attempt = $3, error_code = $4, error_message = $5, updated_at = now()
                    WHERE job_id = $1::uuid AND step_code = $2
                    """,
                    job_id, step_code, attempt, error_code, error_message
                )
            else:
                # Insert new
                await conn.execute(
                    """
                    INSERT INTO studio_job_steps (job_id, step_code, status, attempt, error_code, error_message)
                    VALUES ($1::uuid, $2, 'failed', $3, $4, $5)
                    """,
                    job_id, step_code, attempt, error_code, error_message
                )

    async def list_steps(self, job_id: str) -> list[asyncpg.Record]:
        q = "SELECT * FROM studio_job_steps WHERE job_id=$1::uuid ORDER BY created_at ASC"
        async with self.pool.acquire() as conn:
            return await conn.fetch(q, job_id)