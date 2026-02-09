from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from app.db import get_pool


def _jsonb_param(x: Any) -> str:
    """
    Return a JSON string for safe binding into $N::jsonb.
    This avoids asyncpg DataError when it expects str but receives dict.

    Accepts: dict/list/None/str/other primitives.
    """
    if x is None:
        return "{}"
    if isinstance(x, str):
        s = x.strip()
        return s if s else "{}"
    if isinstance(x, (dict, list)):
        return json.dumps(x, ensure_ascii=False)
    # fallback: wrap unknown types
    return json.dumps({"value": x}, default=str, ensure_ascii=False)


class MusicJobsRepo:
    # -------- music_video_jobs (user-visible job_id) --------
    async def create_video_job(self, *, project_id: UUID, input_json: Dict[str, Any] | None = None) -> UUID:
        jid = uuid4()
        pool = await get_pool()
        await pool.execute(
            """
            insert into music_video_jobs(id, project_id, status, progress, input_json)
            values($1, $2, 'queued', 0, $3::jsonb)
            """,
            jid,
            project_id,
            _jsonb_param(input_json),
        )
        return jid

    async def get_video_job(self, *, job_id: UUID) -> Optional[dict]:
        pool = await get_pool()
        row = await pool.fetchrow("select * from music_video_jobs where id=$1", job_id)
        return dict(row) if row else None

    async def list_queued_video_jobs(self, *, limit: int = 10) -> list[dict]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            select * from music_video_jobs
            where status='queued'
            order by created_at asc
            limit $1
            """,
            limit,
        )
        return [dict(r) for r in rows]

    async def set_video_job_running(self, *, job_id: UUID) -> None:
        pool = await get_pool()
        await pool.execute(
            "update music_video_jobs set status='running', updated_at=now() where id=$1",
            job_id,
        )

    async def set_video_job_progress(self, *, job_id: UUID, progress: int) -> None:
        p = max(0, min(100, int(progress)))
        pool = await get_pool()
        await pool.execute(
            "update music_video_jobs set progress=$2, updated_at=now() where id=$1",
            job_id,
            p,
        )

    async def set_video_job_input_json(self, *, job_id: UUID, input_json: Dict[str, Any]) -> None:
        pool = await get_pool()
        await pool.execute(
            "update music_video_jobs set input_json=$2::jsonb, updated_at=now() where id=$1",
            job_id,
            _jsonb_param(input_json),
        )

    async def set_video_job_failed(self, *, job_id: UUID, error: str) -> None:
        pool = await get_pool()
        await pool.execute(
            """
            update music_video_jobs
            set status='failed', error=$2, updated_at=now()
            where id=$1
            """,
            job_id,
            error,
        )

    async def set_video_job_succeeded(
        self,
        *,
        job_id: UUID,
        preview_video_asset_id: UUID | None = None,
        final_video_asset_id: UUID | None = None,
        performer_a_video_asset_id: UUID | None = None,
        performer_b_video_asset_id: UUID | None = None,
    ) -> None:
        pool = await get_pool()
        await pool.execute(
            """
            update music_video_jobs
            set status='succeeded',
                progress=100,
                performer_a_video_asset_id=coalesce($2, performer_a_video_asset_id),
                performer_b_video_asset_id=coalesce($3, performer_b_video_asset_id),
                preview_video_asset_id=coalesce($4, preview_video_asset_id),
                final_video_asset_id=coalesce($5, final_video_asset_id),
                updated_at=now()
            where id=$1
            """,
            job_id,
            performer_a_video_asset_id,
            performer_b_video_asset_id,
            preview_video_asset_id,
            final_video_asset_id,
        )


    async def claim_video_jobs(self, *, limit: int = 10, stale_after_secs: int | None = None) -> list[dict]:
        pool = await get_pool()

        if stale_after_secs and int(stale_after_secs) > 0:
            secs = int(stale_after_secs)
            rows = await pool.fetch(
                """
                with cte as (
                select id
                from music_video_jobs
                where status='queued'
                    or (status='running' and updated_at < now() - ($2::int * interval '1 second'))
                order by created_at asc
                for update skip locked
                limit $1
                )
                update music_video_jobs j
                set status='running',
                    error=null,
                    updated_at=now()
                from cte
                where j.id = cte.id
                returning j.*
                """,
                limit,
                secs,
            )
        else:
            rows = await pool.fetch(
                """
                with cte as (
                select id
                from music_video_jobs
                where status='queued'
                order by created_at asc
                for update skip locked
                limit $1
                )
                update music_video_jobs j
                set status='running',
                    error=null,
                    updated_at=now()
                from cte
                where j.id = cte.id
                returning j.*
                """,
                limit,
            )

        return [dict(r) for r in rows]


    # -------- music_compose_jobs (internal stitch job) --------
    async def create_compose_job(
        self,
        *,
        user_id: UUID,
        project_id: UUID,
        performer_videos: Dict[str, Any],
        audio_master_url: str,
        exports: list[str] | None = None,
        burn_captions: bool = True,
        camera_edit: str = "beat_cut",
        band_pack: list[str] | None = None,
        auth_token: str | None = None,
        source_job_id: UUID | None = None,
    ) -> UUID:
        cid = uuid4()
        pool = await get_pool()
        await pool.execute(
            """
            insert into music_compose_jobs(
              id, user_id, status, progress, project_id, source_job_id, auth_token,
              performer_videos, audio_master_url, exports, burn_captions, camera_edit, band_pack
            )
            values($1,$2,'queued',0,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11)
            """,
            cid,
            user_id,
            project_id,
            source_job_id,
            auth_token,
            _jsonb_param(performer_videos),     # jsonb-safe
            audio_master_url,
            exports or ["9:16"],
            burn_captions,
            camera_edit,
            band_pack or [],
        )
        return cid  # caller can track stitch progress via music_video_jobs linked by source_job_id