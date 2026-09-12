from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.config import settings
from app.db import get_pool
from desifaces_shared.identity import AccountContextNotFound, resolve_account_context

router = APIRouter(prefix="/api/audio", tags=["audio-v3-output"])


class CanonicalAudioOutputView(BaseModel):
    media_id: UUID
    audio_url: str
    content_type: str | None = None
    bytes: int | None = None


class AudioReadUrlView(BaseModel):
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


def _storage_credentials() -> tuple[str, str]:
    conn = str(settings.AZURE_STORAGE_CONNECTION_STRING or "").strip()
    parts = dict(item.split("=", 1) for item in conn.split(";") if "=" in item)
    account_name = str(parts.get("AccountName") or "").strip()
    account_key = str(parts.get("AccountKey") or "").strip()
    if not account_name or not account_key:
        raise HTTPException(status_code=503, detail="audio_storage_signing_not_configured")
    return account_name, account_key


def _sign_audio_blob(*, container: str, blob_name: str) -> str:
    account_name, account_key = _storage_credentials()
    ttl_seconds = max(60, int(getattr(settings, "AUDIO_SAS_TTL_SECONDS", 3600)))
    token = generate_blob_sas(
        account_name=account_name,
        account_key=account_key,
        container_name=container,
        blob_name=blob_name.lstrip("/"),
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )
    return f"https://{account_name}.blob.core.windows.net/{container}/{blob_name.lstrip('/')}?{token}"


async def _resolve_account_or_401(conn, user_id: UUID):
    try:
        return await resolve_account_context(conn, user_id)
    except AccountContextNotFound as exc:
        raise HTTPException(status_code=401, detail="account_context_not_found") from exc


@router.get("/jobs/{job_id}/canonical-output", response_model=CanonicalAudioOutputView)
async def get_canonical_audio_output(
    job_id: UUID,
    project_id: UUID = Query(...),
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> CanonicalAudioOutputView:
    try:
        canonical_user_id = UUID(str(user_id))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid_user_identity") from exc

    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        project_ok = await conn.fetchval(
            "select exists(select 1 from public.v3_projects where project_id=$1 and account_id=$2)",
            project_id,
            account.account_id,
        )
        if not project_ok:
            raise HTTPException(status_code=404, detail="project_not_found")

        job = await conn.fetchrow(
            """
            select id,status,user_id
            from public.studio_jobs
            where id=$1 and user_id=$2::uuid and studio_type='audio'
            """,
            job_id,
            canonical_user_id,
        )
        if not job:
            raise HTTPException(status_code=404, detail="audio_job_not_found")
        if str(job["status"] or "").lower() != "succeeded":
            raise HTTPException(status_code=409, detail="audio_job_not_succeeded")

        artifact = await conn.fetchrow(
            """
            select id,url,content_type,bytes,sha256,meta_json
            from public.artifacts
            where job_id=$1 and kind='audio'
            order by created_at desc limit 1
            """,
            job_id,
        )
        if not artifact:
            raise HTTPException(status_code=409, detail="audio_job_missing_output")
        artifact_meta = _as_dict(artifact["meta_json"])
        container = str(artifact_meta.get("storage_container") or getattr(settings, "AUDIO_OUTPUT_CONTAINER", "") or "").strip()
        storage_path = str(artifact_meta.get("storage_path") or artifact_meta.get("blob_name") or "").strip()
        if not storage_path:
            artifact_url = str(artifact["url"] or "").strip()
            try:
                parsed = urlparse(artifact_url)
                parts = [part for part in (parsed.path or "").split("/") if part]
                if parsed.scheme in {"http", "https"} and parsed.netloc.endswith(".blob.core.windows.net") and len(parts) >= 2:
                    container = parts[0]
                    storage_path = "/".join(parts[1:])
            except Exception:
                storage_path = ""
        if not container:
            raise HTTPException(status_code=503, detail="audio_output_container_not_configured")
        if not storage_path:
            raise HTTPException(status_code=409, detail="audio_output_missing_storage_path")

        # Strong idempotency is provenance-based rather than URL/SAS based.
        existing = await conn.fetchrow(
            """
            select id,content_type,bytes,account_id,project_id,meta_json
            from public.media_assets
            where user_id=$1
              and kind='audio'
              and meta_json->>'source_audio_artifact_id'=$2
              and lifecycle_state='active'
            order by created_at desc limit 1
            """,
            canonical_user_id,
            str(artifact["id"]),
        )
        if existing:
            if existing["account_id"] and UUID(str(existing["account_id"])) != account.account_id:
                raise HTTPException(status_code=409, detail="audio_canonical_media_account_mismatch")
            if existing["project_id"] and UUID(str(existing["project_id"])) != project_id:
                raise HTTPException(status_code=409, detail="audio_canonical_media_project_mismatch")
            await conn.execute(
                """
                update public.media_assets
                set account_id=coalesce(account_id,$2),
                    project_id=coalesce(project_id,$3),
                    updated_at=now()
                where id=$1
                """,
                existing["id"],
                account.account_id,
                project_id,
            )
            media_id = UUID(str(existing["id"]))
            content_type = existing["content_type"]
            byte_count = existing["bytes"]
        else:
            source_sha = str(artifact["sha256"] or "").strip() or None
            reusable = None
            if source_sha:
                reusable = await conn.fetchrow(
                    """
                    select id,account_id,project_id,content_type,bytes
                    from public.media_assets
                    where user_id=$1 and sha256=$2 and lifecycle_state='active'
                    order by created_at desc limit 1
                    """,
                    canonical_user_id,
                    source_sha,
                )
            # Canonical V3 media identity is provenance-scoped. If an identical binary
            # already exists for a different legacy artifact, create a distinct media row
            # with sha256 left NULL rather than overwriting that row's source lineage. The
            # same legacy artifact is already handled idempotently by source_audio_artifact_id.
            canonical_sha = source_sha if reusable is None else None
            row = await conn.fetchrow(
                """
                insert into public.media_assets(
                  user_id,kind,storage_ref,content_type,bytes,sha256,meta_json,
                  account_id,project_id,role,lifecycle_state
                ) values($1,'audio',$2,$3,$4,$5,$6::jsonb,$7,$8,'preview','active')
                returning id
                """,
                canonical_user_id,
                f"azure://{container}/{storage_path.lstrip('/')}",
                artifact["content_type"],
                artifact["bytes"],
                canonical_sha,
                json.dumps(
                    {
                        "source": "svc-audio",
                        "source_studio_job_id": str(job_id),
                        "source_audio_artifact_id": str(artifact["id"]),
                        "source_sha256": source_sha,
                        "storage_container": container,
                        "storage_path": storage_path,
                    }
                ),
                account.account_id,
                project_id,
            )
            media_id = UUID(str(row["id"]))
            content_type = artifact["content_type"]
            byte_count = artifact["bytes"]

    return CanonicalAudioOutputView(
        media_id=media_id,
        audio_url=_sign_audio_blob(container=container, blob_name=storage_path),
        content_type=content_type,
        bytes=byte_count,
    )


@router.get("/assets/{media_id}/read-url", response_model=AudioReadUrlView)
async def get_audio_media_read_url(
    media_id: UUID,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> AudioReadUrlView:
    try:
        canonical_user_id = UUID(str(user_id))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid_user_identity") from exc

    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        row = await conn.fetchrow(
            """
            select id,account_id,meta_json
            from public.media_assets
            where id=$1 and user_id=$2 and kind='audio' and lifecycle_state='active'
            """,
            media_id,
            canonical_user_id,
        )
        if not row or (row["account_id"] and UUID(str(row["account_id"])) != account.account_id):
            raise HTTPException(status_code=404, detail="audio_media_not_found")
        meta = _as_dict(row["meta_json"])
        container = str(meta.get("storage_container") or getattr(settings, "AUDIO_OUTPUT_CONTAINER", "") or "").strip()
        blob_name = str(meta.get("storage_path") or "").strip()
        if not container or not blob_name:
            raise HTTPException(status_code=409, detail="audio_media_storage_lineage_missing")

    return AudioReadUrlView(
        media_id=media_id,
        read_url=_sign_audio_blob(container=container, blob_name=blob_name),
    )
