from __future__ import annotations

import asyncio
import json
import hashlib
import os
import tempfile
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_id, get_db_pool_dep as get_db_pool
from app.config import settings
from app.services.sas_service import AzureBlobService
from app.services.stitch_service import stitch_video_urls, upload_final_mp4
from desifaces_shared.identity import AccountContextNotFound, resolve_account_context

router = APIRouter(prefix="/api/longform/v3", tags=["longform-v3-scene-stitch"])


class SceneStitchIn(BaseModel):
    project_id: UUID
    workflow_id: UUID
    stage_run_id: UUID
    attempt_id: UUID
    segment_urls: list[str] = Field(min_length=1, max_length=200)


class SceneStitchOut(BaseModel):
    media_id: UUID
    video_url: str
    segment_count: int
    reused: bool = False


class VideoReadUrlOut(BaseModel):
    media_id: UUID
    read_url: str


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _file_sha256_and_size(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _sign_video(*, container: str, blob_name: str) -> str:
    sas = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)
    return sas.sign_read_url(
        container,
        blob_name,
        int(getattr(settings, "FINAL_SAS_TTL_SECONDS", 86400)),
    )


async def _resolve_account_or_401(conn, user_id: UUID):
    try:
        return await resolve_account_context(conn, user_id)
    except AccountContextNotFound as exc:
        raise HTTPException(status_code=401, detail="account_context_not_found") from exc


@router.post("/scene-stitch", response_model=SceneStitchOut)
async def stitch_scene(
    body: SceneStitchIn,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> SceneStitchOut:
    try:
        canonical_user_id = UUID(str(user_id))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid_user_identity") from exc

    segment_urls = [str(value or "").strip() for value in body.segment_urls if str(value or "").strip()]
    if len(segment_urls) != len(body.segment_urls):
        raise HTTPException(status_code=422, detail="scene_stitch_segment_url_required")

    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        workflow = await conn.fetchrow(
            """
            select w.workflow_id,w.project_id,s.stage_run_id,s.stage_type,s.scope_type
            from public.v3_studio_workflows w
            join public.v3_studio_stage_runs s on s.workflow_id=w.workflow_id
            where w.workflow_id=$1 and w.project_id=$2 and w.account_id=$3
              and s.stage_run_id=$4 and s.stage_type='fusion' and s.scope_type='scene'
            """,
            body.workflow_id,
            body.project_id,
            account.account_id,
            body.stage_run_id,
        )
        if not workflow:
            raise HTTPException(status_code=404, detail="scene_stitch_workflow_stage_not_found")

        attempt_ok = await conn.fetchval(
            """
            select exists(
              select 1 from public.v3_studio_stage_attempts a
              where a.attempt_id=$1 and a.stage_run_id=$2
            )
            """,
            body.attempt_id,
            body.stage_run_id,
        )
        if not attempt_ok:
            raise HTTPException(status_code=404, detail="scene_stitch_attempt_not_found")

        existing = await conn.fetchrow(
            """
            select id,meta_json
            from public.media_assets
            where user_id=$1 and account_id=$2 and project_id=$3 and kind='video'
              and lifecycle_state='active'
              and meta_json->>'v3_studio_attempt_id'=$4
              and meta_json->>'v3_studio_stage_run_id'=$5
            order by created_at desc limit 1
            """,
            canonical_user_id,
            account.account_id,
            body.project_id,
            str(body.attempt_id),
            str(body.stage_run_id),
        )
        if existing:
            meta = _as_dict(existing["meta_json"])
            container = str(meta.get("storage_container") or getattr(settings, "AZURE_VIDEO_OUTPUT_CONTAINER", "") or "").strip()
            blob_name = str(meta.get("storage_path") or "").strip()
            if not container or not blob_name:
                raise HTTPException(status_code=409, detail="scene_stitch_existing_media_storage_missing")
            return SceneStitchOut(
                media_id=UUID(str(existing["id"])),
                video_url=_sign_video(container=container, blob_name=blob_name),
                segment_count=len(segment_urls),
                reused=True,
            )

    # CPU/network-heavy stitch/upload runs off the event loop. The deterministic
    # storage path makes a replay safe even if the first HTTP response was lost.
    container = str(getattr(settings, "AZURE_VIDEO_OUTPUT_CONTAINER", "") or "").strip()
    if not container:
        raise HTTPException(status_code=503, detail="video_output_container_not_configured")
    storage_path = f"v3/story-scene/{body.workflow_id}/{body.stage_run_id}/{body.attempt_id}.mp4"

    with tempfile.TemporaryDirectory(prefix="df_v3_scene_stitch_") as td:
        out_mp4 = os.path.join(td, "scene.mp4")
        try:
            await asyncio.to_thread(stitch_video_urls, segment_urls, out_mp4)
            sha256, byte_count = await asyncio.to_thread(_file_sha256_and_size, out_mp4)
            uploaded_storage_path, signed_url = await asyncio.to_thread(
                upload_final_mp4,
                out_mp4,
                storage_path=storage_path,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"scene_stitch_failed:{str(exc)[:1200]}") from exc

    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        # Recheck idempotency after the expensive work to handle concurrent/replayed calls.
        existing = await conn.fetchrow(
            """
            select id,meta_json from public.media_assets
            where user_id=$1 and account_id=$2 and project_id=$3 and kind='video'
              and lifecycle_state='active'
              and meta_json->>'v3_studio_attempt_id'=$4
              and meta_json->>'v3_studio_stage_run_id'=$5
            order by created_at desc limit 1
            """,
            canonical_user_id,
            account.account_id,
            body.project_id,
            str(body.attempt_id),
            str(body.stage_run_id),
        )
        if existing:
            media_id = UUID(str(existing["id"]))
        else:
            reusable = await conn.fetchrow(
                """
                select id,account_id,project_id from public.media_assets
                where user_id=$1 and sha256=$2 and lifecycle_state='active'
                order by created_at desc limit 1
                """,
                canonical_user_id,
                sha256,
            )
            # The same rendered bytes can legitimately be produced by a different
            # logical scene attempt. Preserve attempt-specific provenance instead of
            # colliding with the user+sha uniqueness guard on media_assets.
            canonical_sha = sha256 if reusable is None else None
            row = await conn.fetchrow(
                """
                insert into public.media_assets(
                  user_id,kind,storage_ref,content_type,bytes,sha256,meta_json,
                  account_id,project_id,role,lifecycle_state
                ) values($1,'video',$2,'video/mp4',$3,$4,$5::jsonb,$6,$7,'preview','active')
                returning id
                """,
                canonical_user_id,
                f"azure://{container}/{uploaded_storage_path.lstrip('/')}",
                byte_count,
                canonical_sha,
                json.dumps({
                    "source": "svc-fusion-extension",
                    "source_kind": "v3_scene_stitch",
                    "v3_workflow_id": str(body.workflow_id),
                    "v3_studio_stage_run_id": str(body.stage_run_id),
                    "v3_studio_attempt_id": str(body.attempt_id),
                    "segment_count": len(segment_urls),
                    "storage_container": container,
                    "storage_path": uploaded_storage_path,
                    "source_sha256": sha256,
                }),
                account.account_id,
                body.project_id,
            )
            media_id = UUID(str(row["id"]))

    return SceneStitchOut(
        media_id=media_id,
        video_url=signed_url,
        segment_count=len(segment_urls),
        reused=False,
    )


@router.get("/assets/{media_id}/read-url", response_model=VideoReadUrlOut)
async def get_v3_video_read_url(
    media_id: UUID,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> VideoReadUrlOut:
    try:
        canonical_user_id = UUID(str(user_id))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid_user_identity") from exc

    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        row = await conn.fetchrow(
            """
            select id,account_id,meta_json from public.media_assets
            where id=$1 and user_id=$2 and kind='video' and lifecycle_state='active'
            """,
            media_id,
            canonical_user_id,
        )
        if not row or (row["account_id"] and UUID(str(row["account_id"])) != account.account_id):
            raise HTTPException(status_code=404, detail="video_media_not_found")
        meta = _as_dict(row["meta_json"])
        container = str(meta.get("storage_container") or getattr(settings, "AZURE_VIDEO_OUTPUT_CONTAINER", "") or "").strip()
        blob_name = str(meta.get("storage_path") or "").strip()
        if not container or not blob_name:
            raise HTTPException(status_code=409, detail="video_media_storage_lineage_missing")

    return VideoReadUrlOut(media_id=media_id, read_url=_sign_video(container=container, blob_name=blob_name))
