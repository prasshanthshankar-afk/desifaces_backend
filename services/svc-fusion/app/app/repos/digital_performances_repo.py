from __future__ import annotations

import json
from typing import Any, Dict, Optional

import asyncpg
from asyncpg import UniqueViolationError


def _jsonb(val: Optional[Dict[str, Any]]) -> str:
    return json.dumps(val or {}, ensure_ascii=False)


class DigitalPerformancesRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def upsert_performance(
        self,
        *,
        user_id: str,
        provider: str,
        provider_job_id: Optional[str],
        status: str,
        share_url: Optional[str],
        meta_json: Optional[Dict[str, Any]] = None,
        face_profile_id: Optional[str] = None,
        audio_clip_id: Optional[str] = None,
        video_asset_id: Optional[str] = None,
    ) -> str:
        """
        DB facts:
          - user_id is uuid NOT NULL
          - provider is text NOT NULL
          - provider_job_id is text NULLABLE
          - UNIQUE INDEX exists: (provider, provider_job_id) WHERE provider_job_id IS NOT NULL

        We avoid ON CONFLICT inference for partial unique indexes by:
          1) INSERT first
          2) if unique violation, UPDATE and RETURN id
        """
        pjid = (provider_job_id or "").strip() or None
        payload_str = _jsonb(meta_json)

        async with self.pool.acquire() as conn:
            # Case A: provider_job_id present -> deterministic upsert by (provider, provider_job_id)
            if pjid is not None:
                insert_q = """
                INSERT INTO public.digital_performances
                    (user_id, provider, provider_job_id, status, share_url, meta_json,
                     face_profile_id, audio_clip_id, video_asset_id)
                VALUES
                    ($1::uuid, $2::text, $3::text, $4::text, $5::text, $6::jsonb,
                     $7::uuid, $8::uuid, $9::uuid)
                RETURNING id::text
                """
                try:
                    return await conn.fetchval(
                        insert_q,
                        user_id,
                        provider,
                        pjid,
                        status,
                        share_url,
                        payload_str,
                        face_profile_id,
                        audio_clip_id,
                        video_asset_id,
                    )
                except asyncpg.exceptions.UniqueViolationError:
                    # Someone already inserted this provider/provider_job_id; update it.
                    update_q = """
                    UPDATE public.digital_performances
                    SET
                        user_id = $1::uuid,
                        status = $4::text,
                        share_url = COALESCE($5::text, share_url),
                        face_profile_id = COALESCE($7::uuid, face_profile_id),
                        audio_clip_id = COALESCE($8::uuid, audio_clip_id),
                        video_asset_id = COALESCE($9::uuid, video_asset_id),
                        meta_json = COALESCE(meta_json, '{}'::jsonb) || $6::jsonb,
                        updated_at = now()
                    WHERE provider = $2::text
                      AND provider_job_id = $3::text
                    RETURNING id::text
                    """
                    return await conn.fetchval(
                        update_q,
                        user_id,
                        provider,
                        pjid,
                        status,
                        share_url,
                        payload_str,
                        face_profile_id,
                        audio_clip_id,
                        video_asset_id,
                    )

            # Case B: provider_job_id is NULL -> cannot use unique index; insert a new row
            q2 = """
            INSERT INTO public.digital_performances
                (user_id, provider, provider_job_id, status, share_url, meta_json,
                 face_profile_id, audio_clip_id, video_asset_id)
            VALUES
                ($1::uuid, $2::text, NULL, $3::text, $4::text, $5::jsonb,
                 $6::uuid, $7::uuid, $8::uuid)
            RETURNING id::text
            """
            return await conn.fetchval(
                q2,
                user_id,
                provider,
                status,
                share_url,
                payload_str,
                face_profile_id,
                audio_clip_id,
                video_asset_id,
            )

    async def mark_ready(
        self,
        performance_id: str,
        *,
        share_url: Optional[str],
        meta_json: Optional[Dict[str, Any]] = None,
        video_asset_id: Optional[str] = None,
    ) -> None:
        q = """
        UPDATE public.digital_performances
        SET
            status = 'ready',
            share_url = COALESCE($2::text, share_url),
            video_asset_id = COALESCE($3::uuid, video_asset_id),
            meta_json = COALESCE(meta_json, '{}'::jsonb) || $4::jsonb,
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, performance_id, share_url, video_asset_id, _jsonb(meta_json))

    async def mark_failed(
        self,
        performance_id: str,
        *,
        error_code: str,
        error_message: str,
        meta_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = dict(meta_json or {})
        payload.update({"error_code": error_code, "error_message": error_message})

        q = """
        UPDATE public.digital_performances
        SET
            status = 'failed',
            meta_json = COALESCE(meta_json, '{}'::jsonb) || $2::jsonb,
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, performance_id, _jsonb(payload))


    # -----------------------------
    # Fusion Job Output linking
    # -----------------------------
    async def upsert_fusion_job_output(self, job_id: str, performance_id: str) -> None:
        """
        Link fusion job -> digital performance without ON CONFLICT (avoids constraint inference issues).

        Strategy:
        1) UPDATE existing row by job_id
        2) If nothing updated -> INSERT
        3) If concurrent insert happens -> UPDATE again
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                q_update = """
                UPDATE public.fusion_job_outputs
                SET digital_performance_id = $2::uuid
                WHERE job_id = $1::uuid
                """
                res = await conn.execute(q_update, job_id, performance_id)

                # asyncpg returns "UPDATE <n>"
                if res.startswith("UPDATE 0"):
                    q_insert = """
                    INSERT INTO public.fusion_job_outputs (job_id, digital_performance_id)
                    VALUES ($1::uuid, $2::uuid)
                    """
                    try:
                        await conn.execute(q_insert, job_id, performance_id)
                    except UniqueViolationError:
                        # Another worker/thread inserted first — update again
                        await conn.execute(q_update, job_id, performance_id)