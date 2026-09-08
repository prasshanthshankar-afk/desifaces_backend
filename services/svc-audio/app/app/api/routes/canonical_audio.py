from __future__ import annotations

import json
from typing import Any, Dict, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.db import get_pool

router = APIRouter(prefix="/api/audio", tags=["audio-canonical-output"])


class CanonicalAudioOutputResponse(BaseModel):
    media_id: str
    audio_url: str


class AudioReadUrlResponse(BaseModel):
    read_url: str


def _jsonb_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        parsed = dict(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _storage_ref_from_artifact(meta: Dict[str, Any]) -> str:
    return str(meta.get("storage_path") or "").strip()


async def _authorized_project_account(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    user_id: str,
) -> Optional[str]:
    return await conn.fetchval(
        """
        select p.account_id::text
        from public.v3_projects p
        join public.pricing_billing_account_members bam
          on bam.billing_account_id = p.account_id
         and bam.user_id = $2::uuid
         and bam.status = 'active'
        where p.project_id = $1::uuid
        limit 1
        """,
        project_id,
        user_id,
    )


@router.get("/jobs/{job_id}/canonical-output", response_model=CanonicalAudioOutputResponse)
async def get_canonical_audio_output(
    job_id: str,
    project_id: str = Query(...),
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> CanonicalAudioOutputResponse:
    """Return the durable V3 media identity for a completed Audio job.

    svc-audio owns synthesis and canonical media registration. Director consumes
    only the resulting media_id + audio_url and performs its own account/project
    ownership validation before attaching the output to a studio stage.
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                select id::text, status, user_id::text
                from public.studio_jobs
                where id = $1::uuid
                  and user_id = $2::uuid
                  and studio_type = 'audio'
                """,
                job_id,
                user_id,
            )
            if not job:
                raise HTTPException(status_code=404, detail="job_not_found")

            status_value = str(job["status"] or "").strip().lower()
            if status_value not in {"succeeded", "completed"}:
                raise HTTPException(status_code=409, detail="audio_output_not_ready")

            account_id = await _authorized_project_account(
                conn,
                project_id=project_id,
                user_id=user_id,
            )
            if not account_id:
                raise HTTPException(status_code=403, detail="project_not_accessible")

            artifact = await conn.fetchrow(
                """
                select id::text as artifact_id,
                       url,
                       content_type,
                       bytes,
                       sha256,
                       meta_json
                from public.artifacts
                where job_id = $1::uuid
                  and kind = 'audio'
                order by created_at desc
                limit 1
                """,
                job_id,
            )
            if not artifact or not str(artifact["url"] or "").strip():
                raise HTTPException(status_code=409, detail="audio_output_missing")

            artifact_meta = _jsonb_dict(artifact["meta_json"])
            storage_ref = _storage_ref_from_artifact(artifact_meta)
            if not storage_ref:
                raise HTTPException(status_code=409, detail="audio_storage_ref_missing")

            artifact_id = str(artifact["artifact_id"])
            artifact_sha = str(artifact["sha256"] or "").strip()
            audio_url = str(artifact["url"]).strip()

            media = await conn.fetchrow(
                """
                select id::text, account_id::text, project_id::text, meta_json
                from public.media_assets
                where user_id = $1::uuid
                  and kind = 'audio'
                  and (
                    meta_json->>'source_audio_artifact_id' = $2
                    or ($3 <> '' and sha256 = $3)
                  )
                order by created_at desc
                limit 1
                """,
                user_id,
                artifact_id,
                artifact_sha,
            )

            if media:
                existing_account_id = str(media["account_id"] or "").strip()
                if existing_account_id and existing_account_id != str(account_id):
                    raise HTTPException(status_code=409, detail="audio_media_account_mismatch")
                media_id = str(media["id"])
            else:
                canonical_meta = {
                    **artifact_meta,
                    "source_audio_artifact_id": artifact_id,
                    "source_audio_job_id": str(job_id),
                    "source_audio_url": audio_url,
                    "requested_project_id": str(project_id),
                    "canonical_owner": "svc-audio",
                }
                media_id = await conn.fetchval(
                    """
                    insert into public.media_assets(
                        user_id,
                        kind,
                        storage_ref,
                        content_type,
                        bytes,
                        sha256,
                        meta_json,
                        account_id,
                        project_id,
                        role,
                        lifecycle_state,
                        created_at,
                        updated_at
                    )
                    values(
                        $1::uuid,
                        'audio',
                        $2,
                        $3,
                        $4,
                        nullif($5, ''),
                        $6::jsonb,
                        $7::uuid,
                        null,
                        'intermediate',
                        'active',
                        now(),
                        now()
                    )
                    on conflict (user_id, sha256) where sha256 is not null
                    do update set
                        account_id = coalesce(public.media_assets.account_id, excluded.account_id),
                        meta_json = public.media_assets.meta_json || excluded.meta_json,
                        updated_at = now()
                    returning id::text
                    """,
                    user_id,
                    storage_ref,
                    artifact["content_type"],
                    artifact["bytes"],
                    artifact_sha,
                    json.dumps(canonical_meta, default=str),
                    account_id,
                )

            if not media_id:
                raise HTTPException(status_code=500, detail="canonical_media_registration_failed")

    return CanonicalAudioOutputResponse(media_id=str(media_id), audio_url=audio_url)


@router.get("/assets/{media_id}/read-url", response_model=AudioReadUrlResponse)
async def get_audio_asset_read_url(
    media_id: str,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> AudioReadUrlResponse:
    """Return the most recent owner-service read URL associated with Audio media.

    This keeps Director's existing retry/resume contract valid. A later storage
    adapter can replace source_audio_url with fresh SAS generation without
    changing the Director API contract.
    """
    async with pool.acquire() as conn:
        media = await conn.fetchrow(
            """
            select meta_json
            from public.media_assets
            where id = $1::uuid
              and user_id = $2::uuid
              and kind = 'audio'
              and lifecycle_state = 'active'
            """,
            media_id,
            user_id,
        )
        if not media:
            raise HTTPException(status_code=404, detail="audio_media_not_found")

    meta = _jsonb_dict(media["meta_json"])
    read_url = str(meta.get("source_audio_url") or "").strip()
    if not read_url:
        raise HTTPException(status_code=409, detail="audio_read_url_missing")
    return AudioReadUrlResponse(read_url=read_url)
