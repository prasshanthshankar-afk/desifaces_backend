from __future__ import annotations

from typing import Any, Dict, Optional
import json
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
    """
    DB is NOT NULL DEFAULT 'auto'. We always return a concrete value.
    """
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
        voice_gender_mode: Optional[str] = None,  # "auto" | "manual"
        voice_gender: Optional[str] = None,       # "male" | "female" | None
    ) -> str:
        voice_cfg_json = json.dumps(voice_cfg or {})
        tags_json = json.dumps(tags or {})
        auth_token_norm = _normalize_bearer(auth_token)

        vg = _norm_gender(voice_gender)
        vgm = _norm_gender_mode(voice_gender_mode)

        # If caller specifies gender explicitly, force manual.
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
            voice_cfg_json,
            tags_json,
            script_text,
            int(total_segments),
            auth_token_norm,
            vgm,
            vg,
        )
        return str(row["id"])

    async def get_job(self, conn: asyncpg.Connection, job_id: str, user_id: str) -> Optional[asyncpg.Record]:
        return await conn.fetchrow(
            """
            select *
            from public.longform_jobs
            where id = $1::uuid and user_id = $2::uuid
            """,
            job_id,
            user_id,
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