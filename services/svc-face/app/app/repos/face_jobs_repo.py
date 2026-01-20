from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .base_repo import BaseRepository
from ..domain.models import StudioJobDB

logger = logging.getLogger(__name__)


class FaceJobsRepo(BaseRepository):
    """Repository for studio_jobs table - handles job lifecycle"""

    def __init__(self, db_pool):
        super().__init__(db_pool)

    async def fetch_row(self, query: str, *args) -> Optional[Any]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch_rows(self, query: str, *args) -> Sequence[Any]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def create_job(
        self,
        user_id: str,
        studio_type: str,
        request_hash: str,
        payload: Dict[str, Any],
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a studio job (idempotent by (user_id, studio_type, request_hash)).

        IMPORTANT FIX:
        - On conflict, we refresh payload/meta and make sure a previously failed job
          is re-queued (and errors cleared), so retries actually run.
        """
        query = """
        INSERT INTO studio_jobs (
            studio_type, status, user_id, request_hash,
            payload_json, meta_json, created_at, updated_at, next_run_at
        )
        VALUES (
            $1, 'queued', $2::uuid, $3,
            $4::jsonb, $5::jsonb, now(), now(), now()
        )
        ON CONFLICT (user_id, studio_type, request_hash)
        DO UPDATE SET
            updated_at = now(),
            payload_json = EXCLUDED.payload_json,
            meta_json = COALESCE(studio_jobs.meta_json, '{}'::jsonb) || EXCLUDED.meta_json,
            next_run_at = now(),
            -- Re-queue only if job is not actively running
            status = CASE
                WHEN studio_jobs.status IN ('failed','cancelled') THEN 'queued'
                WHEN studio_jobs.status = 'queued' THEN 'queued'
                ELSE studio_jobs.status
            END,
            error_code = NULL,
            error_message = NULL
        RETURNING id::text
        """

        payload_jsonb = self.prepare_jsonb_param(payload)
        meta_jsonb = self.prepare_jsonb_param(meta or {})
        user_uuid = self.prepare_uuid_param(user_id)

        # fetch_scalar uses asyncpg.fetchval, which works with RETURNING id::text
        job_id = await self.fetch_scalar(query, studio_type, user_uuid, request_hash, payload_jsonb, meta_jsonb)

        logger.info("Job created/upserted", extra={"job_id": job_id, "user_id": user_id, "studio_type": studio_type})
        return str(job_id)

    async def get_job(self, job_id: str) -> Optional[StudioJobDB]:
        query = "SELECT * FROM studio_jobs WHERE id = $1::uuid"
        row = await self.fetch_row(query, job_id)
        return StudioJobDB(**self.convert_db_row(row)) if row else None

    async def update_job_status(
        self,
        job_id: str,
        status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        meta_patch: Optional[Dict[str, Any]] = None,
        next_run_at: Optional[str] = None,
        *,
        clear_error_on_success: bool = True,
    ) -> None:
        """
        Update status + error + optional meta_json patch.
        - meta_patch merges into meta_json (jsonb || patch)
        - optionally clears error_code/error_message when status is succeeded
        """
        s = (status or "").strip().lower()

        if clear_error_on_success and s in ("succeeded", "success"):
            s = "succeeded"
            # if caller didn't pass errors, keep them cleared
            if error_code is None:
                error_code = None
            if error_message is None:
                error_message = None

        if s in ("failed", "running", "queued", "cancelled", "succeeded"):
            pass
        else:
            # avoid weird statuses
            s = "queued"

        patch_jsonb = None if meta_patch is None else self.prepare_jsonb_param(meta_patch)
        next_run_at_param = next_run_at if (next_run_at and str(next_run_at).strip()) else None

        query = """
        UPDATE studio_jobs
        SET
          status = $2,
          error_code = $3,
          error_message = $4,
          meta_json = CASE
            WHEN $5::jsonb IS NULL THEN meta_json
            ELSE COALESCE(meta_json, '{}'::jsonb) || $5::jsonb
          END,
          next_run_at = COALESCE($6::timestamptz, next_run_at),
          updated_at = now()
        WHERE id = $1::uuid
        """
        await self.execute_command(
            query,
            job_id,
            s,
            error_code,
            error_message,
            patch_jsonb,
            next_run_at_param,
        )

        logger.info(
            "Job status updated",
            extra={
                "job_id": job_id,
                "status": s,
                "error_code": error_code,
                "has_meta_patch": bool(meta_patch),
                "next_run_at": next_run_at_param,
            },
        )

    # ------------------------------------------------------------------
    # Backward-compatible aliases (your orchestrator calls update_status)
    # ------------------------------------------------------------------
    async def update_status(
        self,
        job_id: str,
        status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        meta_patch: Optional[Dict[str, Any]] = None,
        next_run_at: Optional[str] = None,
    ) -> None:
        await self.update_job_status(
            job_id=job_id,
            status=status,
            error_code=error_code,
            error_message=error_message,
            meta_patch=meta_patch,
            next_run_at=next_run_at,
        )

    async def claim_next_jobs(self, studio_type: str = "face", limit: int = 1) -> List[str]:
        """
        Claim jobs ready to run:
        - status queued
        - next_run_at <= now()
        Mark running and increment attempt_count.
        """
        query = """
        WITH claimed_jobs AS (
            SELECT id
            FROM studio_jobs
            WHERE studio_type = $1
              AND status = 'queued'
              AND next_run_at <= now()
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        UPDATE studio_jobs j
        SET
          status = 'running',
          attempt_count = attempt_count + 1,
          updated_at = now()
        FROM claimed_jobs cj
        WHERE j.id = cj.id
        RETURNING j.id::text AS id
        """
        rows = await self.execute_queries(query, studio_type, int(limit))
        return [str(r["id"]) for r in rows]

    async def reschedule_job(self, job_id: str, delay_seconds: int, error_code: str, error_message: str) -> None:
        """
        Put job back to queued with backoff and error recorded.
        """
        query = """
        UPDATE studio_jobs
        SET
          status='queued',
          error_code=$2,
          error_message=$3,
          next_run_at = now() + ($4 || ' seconds')::interval,
          updated_at=now()
        WHERE id=$1::uuid
        """
        await self.execute_command(query, job_id, error_code, error_message, str(int(delay_seconds)))

    async def list_user_jobs(self, user_id: str, limit: int = 20) -> List[StudioJobDB]:
        query = """
        SELECT * FROM studio_jobs
        WHERE user_id = $1::uuid AND studio_type = 'face'
        ORDER BY created_at DESC
        LIMIT $2
        """
        rows = await self.execute_queries(query, user_id, int(limit))
        return [StudioJobDB(**self.convert_db_row(r)) for r in rows]