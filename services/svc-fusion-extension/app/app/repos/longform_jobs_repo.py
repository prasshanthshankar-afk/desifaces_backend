
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import asyncpg


def _normalize_bearer(token: Optional[str]) -> Optional[str]:
    """
    Accept either raw JWT or 'Bearer <jwt>' and store as 'Bearer <jwt>'.
    Returns None if empty.
    """
    t = (token or "").strip()
    if not t:
        return None
    if t.lower().startswith("bearer "):
        return t
    return f"Bearer {t}"


def _norm_gender_mode(v: Optional[str]) -> str:
    s = (v or "").strip().lower()
    if not s:
        return "auto"
    if s not in ("auto", "manual"):
        raise ValueError(f"invalid voice_gender_mode: {v}")
    return s


def _norm_gender(v: Optional[str]) -> Optional[str]:
    s = (v or "").strip().lower()
    if not s:
        return None
    if s not in ("male", "female"):
        raise ValueError(f"invalid voice_gender: {v}")
    return s


def _jsonb(value: Optional[Dict[str, Any]]) -> str:
    return json.dumps(value or {})


class LongformJobsRepo:
    def __init__(self) -> None:
        pass

    async def create_job(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: str,
        face_artifact_id: str,
        script_text: str,
        voice_cfg: Dict[str, Any],
        aspect_ratio: str,
        segment_seconds: int,
        max_segment_seconds: int,
        tags: Dict[str, Any],
        total_segments: int,
        auth_token: Optional[str] = None,
        voice_gender_mode: Optional[str] = None,
        voice_gender: Optional[str] = None,
    ) -> str:
        auth_token_norm = _normalize_bearer(auth_token)
        vg = _norm_gender(voice_gender)
        vgm = _norm_gender_mode(voice_gender_mode)

        if vg:
            vgm = "manual"

        row = await conn.fetchrow(
            """
            insert into public.longform_jobs (
              user_id,
              face_artifact_id,
              aspect_ratio,
              segment_seconds,
              max_segment_seconds,
              voice_cfg,
              tags,
              script_text,
              total_segments,
              completed_segments,
              status,
              auth_token,
              voice_gender_mode,
              voice_gender
            )
            values (
              $1::uuid,
              $2::uuid,
              $3::text,
              $4::int,
              $5::int,
              $6::jsonb,
              $7::jsonb,
              $8::text,
              $9::int,
              0,
              'queued',
              $10::text,
              $11::text,
              $12::text
            )
            returning id
            """,
            user_id,
            face_artifact_id,
            aspect_ratio,
            int(segment_seconds),
            int(max_segment_seconds),
            _jsonb(voice_cfg),
            _jsonb(tags),
            script_text,
            int(total_segments),
            auth_token_norm,
            vgm,
            vg,
        )
        return str(row["id"])

    async def get_job(self, conn: asyncpg.Connection, job_id: str, user_id: Optional[str] = None) -> Optional[asyncpg.Record]:
        if user_id:
            return await conn.fetchrow(
                """
                select *
                from public.longform_jobs
                where id = $1::uuid and user_id = $2::uuid
                """,
                job_id,
                user_id,
            )
        return await conn.fetchrow(
            """
            select *
            from public.longform_jobs
            where id = $1::uuid
            """,
            job_id,
        )

    async def list_jobs(self, conn: asyncpg.Connection, user_id: str, limit: int = 20) -> List[asyncpg.Record]:
        return await conn.fetch(
            """
            select *
            from public.longform_jobs
            where user_id = $1::uuid
            order by created_at desc
            limit $2::int
            """,
            user_id,
            int(limit),
        )

    async def set_status(
        self,
        conn: asyncpg.Connection,
        job_id: str,
        status: str,
        *,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        await conn.execute(
            """
            update public.longform_jobs
            set
              status = $2::text,
              error_code = coalesce($3::text, error_code),
              error_message = case
                when $4::text is null then error_message
                else left($4::text, 4000)
              end
            where id = $1::uuid
            """,
            job_id,
            status,
            error_code,
            error_message,
        )

    async def set_counts(self, conn: asyncpg.Connection, job_id: str, total_segments: int, completed_segments: int) -> None:
        await conn.execute(
            """
            update public.longform_jobs
            set
              total_segments = $2::int,
              completed_segments = $3::int
            where id = $1::uuid
            """,
            job_id,
            int(total_segments),
            int(completed_segments),
        )

    async def bump_completed(self, conn: asyncpg.Connection, job_id: str) -> None:
        await conn.execute(
            """
            update public.longform_jobs
            set
              completed_segments = completed_segments + 1,
              status = case
                when completed_segments + 1 >= total_segments then 'stitching'
                else status
              end
            where id = $1::uuid
            """,
            job_id,
        )

    async def set_final(self, conn: asyncpg.Connection, job_id: str, storage_path: str, signed_url: str) -> None:
        await conn.execute(
            """
            update public.longform_jobs
            set
              final_storage_path = $2::text,
              final_video_url = $3::text
            where id = $1::uuid
            """,
            job_id,
            storage_path,
            signed_url,
        )

    async def update_tags(self, conn: asyncpg.Connection, job_id: str, tags: Dict[str, Any]) -> None:
        await conn.execute(
            """
            update public.longform_jobs
            set tags = $2::jsonb
            where id = $1::uuid
            """,
            job_id,
            _jsonb(tags),
        )

    async def merge_tags(self, conn: asyncpg.Connection, job_id: str, patch: Dict[str, Any]) -> None:
        await conn.execute(
            """
            update public.longform_jobs
            set tags = coalesce(tags, '{}'::jsonb) || $2::jsonb
            where id = $1::uuid
            """,
            job_id,
            _jsonb(patch),
        )

    async def mark_failed(self, conn: asyncpg.Connection, job_id: str, *, error_code: str, error_message: str) -> None:
        await self.set_status(
            conn,
            job_id,
            "failed",
            error_code=error_code,
            error_message=error_message,
        )
