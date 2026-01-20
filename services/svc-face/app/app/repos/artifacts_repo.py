from __future__ import annotations

from typing import Optional, Dict, Any, List
import logging

from .base_repo import BaseRepository

logger = logging.getLogger(__name__)


class ArtifactsRepo(BaseRepository):
    """
    Repository for public.artifacts (job-scoped URLs returned to clients).
    """

    async def create_artifact(
        self,
        job_id: str,
        kind: str,
        url: str,
        content_type: Optional[str] = None,
        sha256: Optional[str] = None,
        bytes_size: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        q = """
        INSERT INTO artifacts (job_id, kind, url, content_type, sha256, bytes, meta_json, created_at)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, now())
        RETURNING id::text
        """
        return await self.fetch_scalar(
            q,
            job_id,
            kind,
            url,
            content_type,
            sha256,
            bytes_size,
            self.prepare_jsonb_param(meta or {}),
        )

    async def list_job_artifacts(
        self,
        job_id: str,
        kind: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        if kind:
            q = """
            SELECT * FROM artifacts
            WHERE job_id=$1::uuid AND kind=$2
            ORDER BY created_at ASC
            LIMIT $3
            """
            rows = await self.execute_queries(q, job_id, kind, limit)
        else:
            q = """
            SELECT * FROM artifacts
            WHERE job_id=$1::uuid
            ORDER BY created_at ASC
            LIMIT $2
            """
            rows = await self.execute_queries(q, job_id, limit)

        return [self.convert_db_row(r) for r in rows]