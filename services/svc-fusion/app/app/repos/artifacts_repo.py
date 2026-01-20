from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import asyncpg


class ArtifactsRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def add_artifact(
        self,
        job_id: str,
        kind: str,
        url: str,
        *,
        content_type: Optional[str] = None,
        meta_json: Optional[Dict[str, Any]] = None,
        sha256: Optional[str] = None,
        bytes: Optional[int] = None,
    ) -> None:
        """
        Add an artifact to the database.

        Matches DB schema (public.artifacts):
          id uuid default gen_random_uuid()
          job_id uuid not null
          kind text not null
          url text not null
          content_type text null
          sha256 text null
          bytes bigint null
          meta_json jsonb not null default '{}'
          created_at timestamptz not null default now()

        Args:
            job_id: UUID of the parent job (studio_jobs.id)
            kind: Type/kind of artifact
            url: URL/location of the artifact (often Azure Blob SAS, HeyGen URL, etc.)
            content_type: Optional MIME type
            meta_json: Optional metadata dict (stored as jsonb)
            sha256: Optional checksum
            bytes: Optional byte length
        """
        q = """
        INSERT INTO public.artifacts (job_id, kind, url, content_type, sha256, bytes, meta_json)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb)
        """

        payload = meta_json if meta_json is not None else {}
        payload_str = json.dumps(payload, ensure_ascii=False)

        async with self.pool.acquire() as conn:
            await conn.execute(q, job_id, kind, url, content_type, sha256, bytes, payload_str)

    async def get_artifact_by_id(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a single artifact row by artifact UUID.

        Returns a dict with:
          id, job_id, kind, url, content_type, sha256, bytes, meta_json, created_at
        """
        q = """
        SELECT
          id::text        AS id,
          job_id::text    AS job_id,
          kind            AS kind,
          url             AS url,
          content_type    AS content_type,
          sha256          AS sha256,
          bytes           AS bytes,
          meta_json       AS meta_json,
          created_at      AS created_at
        FROM public.artifacts
        WHERE id = $1::uuid
        LIMIT 1
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, artifact_id)
            return dict(row) if row else None

    async def get_artifacts_by_job_id(self, job_id: str) -> List[Dict[str, Any]]:
        """
        Fetch all artifact rows for a given job UUID.

        Returns list of dicts with:
          id, job_id, kind, url, content_type, sha256, bytes, meta_json, created_at
        """
        q = """
        SELECT
          id::text        AS id,
          job_id::text    AS job_id,
          kind            AS kind,
          url             AS url,
          content_type    AS content_type,
          sha256          AS sha256,
          bytes           AS bytes,
          meta_json       AS meta_json,
          created_at      AS created_at
        FROM public.artifacts
        WHERE job_id = $1::uuid
        ORDER BY created_at ASC
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, job_id)
            return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Back-compat alias for routes that call list_artifacts(job_id)
    # -------------------------------------------------------------------------
    async def list_artifacts(self, job_id: str) -> List[Dict[str, Any]]:
        """
        Alias used by fusion_jobs.py (/jobs/{job_id}).
        """
        return await self.get_artifacts_by_job_id(job_id)