
from __future__ import annotations

from typing import Any, Dict, List, Optional

import asyncpg


class LongformSegmentsRepo:
    def __init__(self) -> None:
        pass

    async def insert_segment(
        self,
        conn: asyncpg.Connection,
        *,
        job_id: str,
        segment_index: int,
        text_chunk: str,
        duration_sec: int,
    ) -> str:
        row = await conn.fetchrow(
            """
            insert into public.longform_segments (
              job_id, segment_index, status, text_chunk, duration_sec
            )
            values ($1::uuid, $2::int, 'queued', $3::text, $4::int)
            returning id
            """,
            job_id,
            int(segment_index),
            text_chunk,
            int(duration_sec),
        )
        return str(row["id"])

    async def insert_segments_batch(
        self,
        conn: asyncpg.Connection,
        *,
        job_id: str,
        segments: List[Dict[str, Any]],
    ) -> List[str]:
        ids: List[str] = []
        for seg in sorted(segments, key=lambda x: int(x["segment_index"])):
            seg_id = await self.insert_segment(
                conn,
                job_id=job_id,
                segment_index=int(seg["segment_index"]),
                text_chunk=str(seg["text_chunk"]),
                duration_sec=int(seg["duration_sec"]),
            )
            ids.append(seg_id)
        return ids

    async def list_segments_for_job(self, conn: asyncpg.Connection, job_id: str) -> List[asyncpg.Record]:
        return await conn.fetch(
            """
            select *
            from public.longform_segments
            where job_id = $1::uuid
            order by segment_index asc
            """,
            job_id,
        )

    async def list_by_job(self, conn: asyncpg.Connection, job_id: str) -> List[asyncpg.Record]:
        return await self.list_segments_for_job(conn, job_id)

    async def count_done(self, conn: asyncpg.Connection, job_id: str) -> int:
        row = await conn.fetchrow(
            """
            select count(*)::int as cnt
            from public.longform_segments
            where job_id = $1::uuid and status = 'succeeded'
            """,
            job_id,
        )
        return int(row["cnt"] or 0)

    async def any_failed(self, conn: asyncpg.Connection, job_id: str) -> bool:
        row = await conn.fetchrow(
            """
            select exists(
              select 1
              from public.longform_segments
              where job_id = $1::uuid and status = 'failed'
            ) as has_failed
            """,
            job_id,
        )
        return bool(row["has_failed"])

    async def fetch_next_segments(
        self,
        conn: asyncpg.Connection,
        limit: int,
        max_inflight_per_job: int,
    ) -> List[asyncpg.Record]:
        """
        Atomically:
        - pick eligible queued segments
        - strictly enforce per-job inflight cap within the same fetch
        - claim them by setting status='audio_running'
        - return segment rows + job context

        Returns:
        - face_image_url (media_assets.storage_ref / SAS or storage ref depending on your setup)
        - face_meta_json (media_assets.meta_json) for auto gender
        - voice_gender_mode / voice_gender (longform_jobs)
        - voice_cfg (longform_jobs)
        - auth_token (longform_jobs)
        - tags (longform_jobs.tags) for directed-mode metadata lookup
        - job_script_text (longform_jobs.script_text) as fallback
        """
        return await conn.fetch(
            """
            with inflight as (
            select
                job_id,
                count(*) as inflight_cnt
            from public.longform_segments
            where status in ('audio_running', 'video_running')
            group by job_id
            ),
            eligible as (
            select
                s.id,
                s.job_id,
                s.segment_index,
                j.created_at as job_created_at,
                coalesce(i.inflight_cnt, 0) as inflight_cnt,
                greatest($2::int - coalesce(i.inflight_cnt, 0), 0) as available_slots,
                row_number() over (
                partition by s.job_id
                order by s.segment_index asc, s.id asc
                ) as rn
            from public.longform_segments s
            join public.longform_jobs j
                on j.id = s.job_id
            left join inflight i
                on i.job_id = s.job_id
            where s.status = 'queued'
                and j.status in ('queued', 'running')
            ),
            pick as (
            select s.id
            from public.longform_segments s
            join eligible e
                on e.id = s.id
            where e.available_slots > 0
                and e.rn <= e.available_slots
            order by e.job_created_at asc, e.segment_index asc, s.id asc
            limit $1::int
            for update of s skip locked
            ),
            claimed as (
            update public.longform_segments s
            set
                status = 'audio_running',
                locked_at = now(),
                locked_by = 'svc-fusion-extension'
            from pick p
            where s.id = p.id
                and s.status = 'queued'
            returning s.id, s.job_id
            ),
            touched_jobs as (
            update public.longform_jobs j
            set status = 'running'
            from (
                select distinct job_id
                from claimed
            ) cj
            where j.id = cj.job_id
                and j.status = 'queued'
            returning j.id
            )
            select
            s.*,
            j.user_id,
            j.face_artifact_id,
            ma.storage_ref as face_image_url,
            ma.meta_json as face_meta_json,
            j.aspect_ratio,
            j.voice_cfg,
            j.voice_gender_mode,
            j.voice_gender,
            j.auth_token,
            j.tags,
            j.script_text as job_script_text
            from public.longform_segments s
            join claimed c
            on c.id = s.id
            join public.longform_jobs j
            on j.id = s.job_id
            left join public.media_assets ma
            on ma.id = j.face_artifact_id
            order by s.segment_index asc, s.id asc
            """,
            int(limit),
            int(max_inflight_per_job),
        )

    async def save_audio_result(
        self,
        conn: asyncpg.Connection,
        seg_id: str,
        *,
        tts_job_id: str,
        audio_url: str,
        audio_artifact_id: Optional[str] = None,
    ) -> None:
        await conn.execute(
            """
            update public.longform_segments
            set
              status = 'video_running',
              tts_job_id = $2::uuid,
              audio_url = $3::text,
              audio_artifact_id = $4::uuid
            where id = $1::uuid
            """,
            seg_id,
            tts_job_id,
            audio_url,
            audio_artifact_id,
        )

    async def save_fusion_job(self, conn: asyncpg.Connection, seg_id: str, fusion_job_id: str) -> None:
        await conn.execute(
            """
            update public.longform_segments
            set fusion_job_id = $2::uuid
            where id = $1::uuid
            """,
            seg_id,
            fusion_job_id,
        )

    async def mark_succeeded(
        self,
        conn: asyncpg.Connection,
        seg_id: str,
        *,
        segment_video_url: str,
        segment_storage_path: Optional[str],
        provider_job_id: Optional[str],
    ) -> None:
        await conn.execute(
            """
            update public.longform_segments
            set
              status = 'succeeded',
              segment_video_url = $2::text,
              segment_storage_path = $3::text,
              provider_job_id = $4::text,
              locked_at = null,
              locked_by = null
            where id = $1::uuid
            """,
            seg_id,
            segment_video_url,
            segment_storage_path,
            provider_job_id,
        )

    async def mark_failed(
        self,
        conn: asyncpg.Connection,
        seg_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        await conn.execute(
            """
            update public.longform_segments
            set
              status = 'failed',
              error_code = $2::text,
              error_message = left($3::text, 4000),
              locked_at = null,
              locked_by = null
            where id = $1::uuid
            """,
            seg_id,
            error_code,
            error_message or "",
        )

    async def reset_segment_to_queued(self, conn: asyncpg.Connection, seg_id: str) -> None:
        await conn.execute(
            """
            update public.longform_segments
            set
              status = 'queued',
              locked_at = null,
              locked_by = null
            where id = $1::uuid
            """,
            seg_id,
        )
