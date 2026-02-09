from __future__ import annotations

from typing import List, Optional
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

    async def fetch_next_segments(
        self,
        conn: asyncpg.Connection,
        limit: int,
        max_inflight_per_job: int,
    ) -> List[asyncpg.Record]:
        """
        Atomically:
        - pick eligible queued segments (respect inflight cap per job)
        - claim them by setting status='audio_running'
        - return segment rows + job context

        Returns:
          - face_image_url (media_assets.storage_ref SAS)
          - face_meta_json (media_assets.meta_json) for auto gender
          - voice_gender_mode / voice_gender (longform_jobs)
          - voice_cfg (longform_jobs)
          - auth_token (longform_jobs)
        """
        return await conn.fetch(
            """
            with inflight as (
              select job_id, count(*) as inflight_cnt
              from public.longform_segments
              where status in ('audio_running','video_running')
              group by job_id
            ),
            pick as (
              select s.id
              from public.longform_segments s
              join public.longform_jobs j on j.id = s.job_id
              left join inflight i on i.job_id = s.job_id
              where s.status = 'queued'
                and j.status in ('queued','running')
                and coalesce(i.inflight_cnt,0) < $2::int
              order by j.created_at asc, s.segment_index asc
              limit $1::int
              for update skip locked
            ),
            claimed as (
              update public.longform_segments s
              set status = 'audio_running'
              from pick p
              where s.id = p.id
                and s.status = 'queued'
              returning s.id
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
              j.auth_token
            from public.longform_segments s
            join claimed c on c.id = s.id
            join public.longform_jobs j on j.id = s.job_id
            left join public.media_assets ma on ma.id = j.face_artifact_id
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
            set status = 'video_running',
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
        segment_storage_path: str,
        provider_job_id: Optional[str],
    ) -> None:
        await conn.execute(
            """
            update public.longform_segments
            set status = 'succeeded',
                segment_video_url = $2::text,
                segment_storage_path = $3::text,
                provider_job_id = $4::text
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
            set status = 'failed',
                error_code = $2::text,
                error_message = $3::text
            where id = $1::uuid
            """,
            seg_id,
            error_code,
            (error_message or "")[:4000],
        )