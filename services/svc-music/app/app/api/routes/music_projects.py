from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional, Tuple
from urllib.parse import urlparse, parse_qs
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from app.api.deps import get_current_user
from app.config import settings
from app.db import get_pool
from app.domain.models import (
    CreateMusicProjectIn,
    CreateMusicProjectOut,
    MusicProjectOut,
    UpdateMusicProjectIn,
    UpsertMusicPerformerIn,
    MusicPerformerOut,
    UpsertMusicLyricsIn,
    MusicLyricsOut,
    VoiceReferenceOut,
)
from app.repos.music_projects_repo import MusicProjectsRepo
from app.repos.media_assets_repo import MediaAssetsRepo
from app.services.azure_storage_service import AzureStorageService

router = APIRouter(prefix="/music/projects", tags=["music-projects"])

VOICE_REF_MAX_BYTES = 25 * 1024 * 1024  # 25MB
GET_SAS_MIN_VALIDITY_SECONDS = 60       # if SAS is valid for >60s, keep it as-is on GET
PERSIST_MIN_VALIDITY_MINUTES = 30       # avoid churn on persist paths unless expiring soon


def _to_primitive(v):
    """Convert enums (and enum-like) to primitive values for DB binding."""
    if isinstance(v, Enum):
        return v.value
    if hasattr(v, "value") and not isinstance(v, (str, int, float, bool, dict, list, tuple)):
        try:
            return v.value
        except Exception:
            return v
    return v


async def _assert_owner(repo: MusicProjectsRepo, project_id: UUID, user_id: UUID) -> None:
    owner_id = await repo.get_user_id(project_id=project_id)
    if not owner_id or owner_id != user_id:
        raise HTTPException(status_code=404, detail="project_not_found")


def _as_dict(x: Any) -> dict:
    """
    meta_json can be dict (jsonb) OR json-string (legacy asyncpg codec paths).
    """
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        if s.startswith("{") or s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}
    return {}


def _extract_container_and_path_from_meta(meta_json: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    Prefer new semantics stored in meta_json:
      {"container": "music-input", "storage_path": "<blob_path>"}
    """
    m = _as_dict(meta_json)
    c = m.get("container")
    p = m.get("storage_path")
    c = str(c).strip() if isinstance(c, str) and c.strip() else None
    p = str(p).strip() if isinstance(p, str) and p.strip() else None
    return c, p


def _fallback_input_container() -> str:
    return (getattr(settings, "MUSIC_INPUT_CONTAINER", None) or "music-input").strip() or "music-input"


def _fallback_output_container() -> str:
    return (getattr(settings, "MUSIC_OUTPUT_CONTAINER", None) or "music-output").strip() or "music-output"


def _resolve_container_and_path(*, storage_ref: str | None, meta_json: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve (container, blob_path) with backward compatibility:
      1) meta_json.container + meta_json.storage_path (new rows)
      2) parse container + blob_path from storage_ref URL (legacy rows)
      3) if only blob_path known, assume input container first
    """
    meta_container, meta_path = _extract_container_and_path_from_meta(meta_json)

    url_container, url_path = (None, None)
    if storage_ref:
        try:
            url_container, url_path = AzureStorageService.parse_blob_url(storage_ref)
        except Exception:
            url_container, url_path = (None, None)

    path = meta_path or url_path
    container = meta_container or url_container

    if not container and path:
        container = _fallback_input_container() or _fallback_output_container()

    return container, path


def _sas_expires_at(storage_ref: str) -> datetime | None:
    """
    Parse Azure SAS expiry from the `se` query param.
    Returns timezone-aware UTC datetime, or None if not parseable / not present.
    """
    try:
        qs = parse_qs(urlparse(storage_ref).query)
        se = (qs.get("se") or [None])[0]
        if not se:
            return None
        s = str(se).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _looks_like_sas(storage_ref: str) -> bool:
    try:
        q = urlparse(storage_ref).query
        return ("sig=" in q) or ("se=" in q) or ("sp=" in q)
    except Exception:
        return False


def _get_asset_sas_readonly(*, storage_ref: str | None, meta_json: Any) -> str | None:
    """
    GET-SAFE: never writes to DB.
    Behavior:
      - If existing SAS is still valid for >GET_SAS_MIN_VALIDITY_SECONDS, return it as-is.
      - If it's not a SAS (bare blob URL) OR it's expired/near-expiry, generate a fresh SAS.
      - If we can't resolve blob_path, return storage_ref as-is.
    """
    if not storage_ref and not meta_json:
        return storage_ref

    now = datetime.now(timezone.utc)

    if storage_ref:
        # If it's a SAS and still valid, keep it to avoid churn/caching issues.
        exp = _sas_expires_at(storage_ref)
        if exp and exp > (now + timedelta(seconds=GET_SAS_MIN_VALIDITY_SECONDS)):
            return storage_ref

        # If it's clearly a SAS but we can't parse expiry, don't churn; return as-is.
        if _looks_like_sas(storage_ref) and exp is None:
            return storage_ref

    container, blob_path = _resolve_container_and_path(storage_ref=storage_ref, meta_json=meta_json)
    if not blob_path:
        return storage_ref

    container = container or _fallback_input_container()
    try:
        storage = AzureStorageService(container=container)
        return storage.sas_url_for(blob_path)
    except Exception:
        return storage_ref


async def _update_media_asset_refs_best_effort(
    *,
    pool,
    asset_id: UUID,
    new_storage_ref: str,
    container: Optional[str],
    storage_path: Optional[str],
) -> None:
    """
    media_assets.meta_json is JSONB.
    Merge container/storage_path into existing meta_json (best effort), then update storage_ref.
    Never writes meta_json=NULL. Never fails the caller.
    """
    existing_meta: Any = {}
    try:
        r = await pool.fetchrow("select meta_json from public.media_assets where id=$1", asset_id)
        if r and r.get("meta_json") is not None:
            existing_meta = r["meta_json"]
    except Exception:
        existing_meta = {}

    meta_obj = _as_dict(existing_meta)

    if container and not meta_obj.get("container"):
        meta_obj["container"] = container
    if storage_path and not meta_obj.get("storage_path"):
        meta_obj["storage_path"] = storage_path

    try:
        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, meta_json=$3, updated_at=now()
            where id=$1
            """,
            asset_id,
            new_storage_ref,
            meta_obj if meta_obj else {},
        )
        return
    except Exception:
        pass

    # Last resort: try updating just storage_ref (still best-effort)
    try:
        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, updated_at=now()
            where id=$1
            """,
            asset_id,
            new_storage_ref,
        )
    except Exception:
        return


async def _refresh_asset_sas_and_persist(
    *,
    pool,
    asset_id: UUID,
    storage_ref: str | None,
    meta_json: Any,
    min_validity_minutes: int = PERSIST_MIN_VALIDITY_MINUTES,
) -> str | None:
    """
    PERSIST PATH ONLY (POST / job-start / publish):
      - If SAS is valid for >= min_validity_minutes, return as-is (avoid churn).
      - Else generate fresh SAS and persist storage_ref (+ patch meta_json container/path if missing).
    """
    if not storage_ref and not meta_json:
        return storage_ref

    now = datetime.now(timezone.utc)

    if storage_ref:
        exp = _sas_expires_at(storage_ref)
        if exp and (exp - now) >= timedelta(minutes=min_validity_minutes):
            return storage_ref

    container, blob_path = _resolve_container_and_path(storage_ref=storage_ref, meta_json=meta_json)
    if not blob_path:
        return storage_ref

    container = container or _fallback_input_container()

    try:
        storage = AzureStorageService(container=container)
        refreshed_ref = storage.sas_url_for(blob_path)

        await _update_media_asset_refs_best_effort(
            pool=pool,
            asset_id=asset_id,
            new_storage_ref=refreshed_ref,
            container=container,
            storage_path=blob_path,
        )
        return refreshed_ref
    except Exception:
        return storage_ref


async def _set_project_voice_ref_asset_id(
    *, pool, project_id: UUID, user_id: UUID, voice_ref_asset_id: UUID
) -> None:
    """
    Persist the current voice reference mapping on music_projects.
    Uses user_id guard to avoid cross-tenant writes.
    """
    await pool.execute(
        """
        update public.music_projects
        set voice_ref_asset_id=$1, updated_at=now()
        where id=$2 and user_id=$3
        """,
        voice_ref_asset_id,
        project_id,
        user_id,
    )


@router.post("", response_model=CreateMusicProjectOut)
async def create_project(payload: CreateMusicProjectIn, user=Depends(get_current_user)):
    repo = MusicProjectsRepo()

    project_id = await repo.create(
        user_id=user.id,
        title=payload.title,
        mode=_to_primitive(payload.mode),
        duet_layout=_to_primitive(payload.duet_layout),
        language_hint=payload.language_hint,
    )

    style_fields = {
        "scene_pack_id": payload.scene_pack_id,
        "camera_edit": _to_primitive(payload.camera_edit) if payload.camera_edit is not None else None,
        "band_pack": _to_primitive(payload.band_pack) if getattr(payload, "band_pack", None) is not None else None,
    }
    style_fields = {k: v for k, v in style_fields.items() if v is not None}
    if style_fields:
        await repo.update(project_id=project_id, user_id=user.id, **style_fields)

    row = await repo.get(project_id=project_id, user_id=user.id)
    if not row:
        raise HTTPException(status_code=500, detail="project_create_failed")

    return CreateMusicProjectOut(
        project_id=project_id,
        status=row.get("status"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("/{project_id}/voice-reference", response_model=VoiceReferenceOut)
async def upload_voice_reference_for_project(
    project_id: UUID,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    proj_repo = MusicProjectsRepo()
    row = await proj_repo.get(project_id=project_id, user_id=user.id)
    if not row:
        raise HTTPException(status_code=404, detail="project_not_found")

    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="invalid_audio_content_type")

    try:
        data = await file.read()
    finally:
        try:
            await file.close()
        except Exception:
            pass

    if not data:
        raise HTTPException(status_code=400, detail="empty_file")

    if len(data) > VOICE_REF_MAX_BYTES:
        raise HTTPException(status_code=413, detail="voice_reference_too_large")

    sha256_hex = hashlib.sha256(data).hexdigest()
    pool = await get_pool()

    # DEDUPE (idempotent): reuse existing VOICE_REF for (user_id, sha256)
    existing = await pool.fetchrow(
        """
        select id, content_type, bytes, storage_ref, meta_json, created_at
        from public.media_assets
        where user_id=$1 and sha256=$2 and kind='voice_ref'
        limit 1
        """,
        user.id,
        sha256_hex,
    )
    if existing:
        existing_id = UUID(str(existing["id"]))
        existing_ct = existing["content_type"]
        existing_bytes = existing["bytes"]
        existing_storage_ref = str(existing["storage_ref"]) if existing["storage_ref"] else None
        existing_created_at = existing["created_at"]

        refreshed_ref = await _refresh_asset_sas_and_persist(
            pool=pool,
            asset_id=existing_id,
            storage_ref=existing_storage_ref,
            meta_json=existing.get("meta_json"),
        )

        await _set_project_voice_ref_asset_id(
            pool=pool,
            project_id=project_id,
            user_id=user.id,
            voice_ref_asset_id=existing_id,
        )

        return VoiceReferenceOut(
            project_id=project_id,
            voice_ref_asset_id=existing_id,
            content_type=str(existing_ct or file.content_type),
            bytes=int(existing_bytes or len(data)),
            storage_ref=str(refreshed_ref or existing_storage_ref or ""),
            created_at=existing_created_at,
        )

    # New upload path (voice refs are user uploads => music-input)
    filename = (file.filename or "voice_ref.wav").strip()

    ext = "wav"
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower().strip() or "wav"
    if ext == "mpeg" and (file.content_type or "") == "audio/mpeg":
        ext = "mp3"

    storage = AzureStorageService.for_input()  # always music-input
    variant = int(datetime.now(timezone.utc).timestamp())

    res = await storage.upload_bytes(
        data=data,
        user_id=str(user.id),
        scope_id=f"{project_id}/voice_ref",
        variant=variant,
        ext=ext,
        content_type=file.content_type,
    )

    assets_repo = MediaAssetsRepo()
    try:
        voice_ref_asset_id, created_at = await assets_repo.create_asset(
            user_id=user.id,
            kind="voice_ref",
            storage_ref=res.sas_url,
            content_type=file.content_type,
            bytes_len=res.bytes,
            sha256_hex=sha256_hex,
            meta_json={
                "container": storage.container,
                "storage_path": res.storage_path,
                "filename": filename,
                "project_id": str(project_id),
            },
            duration_ms=None,
        )
    except asyncpg.exceptions.UniqueViolationError:
        # Race: someone inserted same sha between our check and insert.
        existing2 = await pool.fetchrow(
            """
            select id, content_type, bytes, storage_ref, meta_json, created_at
            from public.media_assets
            where user_id=$1 and sha256=$2 and kind='voice_ref'
            limit 1
            """,
            user.id,
            sha256_hex,
        )
        if not existing2:
            raise

        existing2_id = UUID(str(existing2["id"]))
        existing2_storage_ref = str(existing2["storage_ref"]) if existing2.get("storage_ref") else None

        refreshed_ref = await _refresh_asset_sas_and_persist(
            pool=pool,
            asset_id=existing2_id,
            storage_ref=existing2_storage_ref,
            meta_json=existing2.get("meta_json"),
        )

        await _set_project_voice_ref_asset_id(
            pool=pool,
            project_id=project_id,
            user_id=user.id,
            voice_ref_asset_id=existing2_id,
        )

        return VoiceReferenceOut(
            project_id=project_id,
            voice_ref_asset_id=existing2_id,
            content_type=str(existing2["content_type"] or file.content_type),
            bytes=int(existing2["bytes"] or len(data)),
            storage_ref=str(refreshed_ref or existing2_storage_ref or res.sas_url),
            created_at=existing2["created_at"],
        )

    await _set_project_voice_ref_asset_id(
        pool=pool,
        project_id=project_id,
        user_id=user.id,
        voice_ref_asset_id=voice_ref_asset_id,
    )

    return VoiceReferenceOut(
        project_id=project_id,
        voice_ref_asset_id=voice_ref_asset_id,
        content_type=file.content_type,
        bytes=res.bytes,
        storage_ref=res.sas_url,
        created_at=created_at,
    )


@router.get("/{project_id}", response_model=MusicProjectOut)
async def get_project(project_id: UUID, user=Depends(get_current_user)):
    repo = MusicProjectsRepo()
    row = await repo.get(project_id=project_id, user_id=user.id)
    if not row:
        raise HTTPException(status_code=404, detail="project_not_found")
    return MusicProjectOut(**row)


@router.patch("/{project_id}", response_model=MusicProjectOut)
async def update_project(project_id: UUID, payload: UpdateMusicProjectIn, user=Depends(get_current_user)):
    repo = MusicProjectsRepo()

    raw = payload.model_dump(exclude_none=True)
    clean = {k: _to_primitive(v) for k, v in raw.items()}

    await repo.update(project_id=project_id, user_id=user.id, **clean)

    row = await repo.get(project_id=project_id, user_id=user.id)
    if not row:
        raise HTTPException(status_code=404, detail="project_not_found")
    return MusicProjectOut(**row)


@router.get("/{project_id}/performers", response_model=list[MusicPerformerOut])
async def list_performers(project_id: UUID, user=Depends(get_current_user)):
    repo = MusicProjectsRepo()
    await _assert_owner(repo, project_id, user.id)

    rows = await repo.get_performers(project_id=project_id)
    return [MusicPerformerOut(**r) for r in rows]


@router.post("/{project_id}/performers", response_model=list[MusicPerformerOut])
async def upsert_performer(project_id: UUID, payload: UpsertMusicPerformerIn, user=Depends(get_current_user)):
    repo = MusicProjectsRepo()
    await _assert_owner(repo, project_id, user.id)

    await repo.upsert_performer(
        project_id=project_id,
        role=_to_primitive(payload.role),
        image_asset_id=payload.image_asset_id,
        voice_mode=_to_primitive(payload.voice_mode),
        user_is_owner=payload.user_is_owner,
    )

    rows = await repo.get_performers(project_id=project_id)
    return [MusicPerformerOut(**r) for r in rows]


@router.get("/{project_id}/lyrics", response_model=MusicLyricsOut)
async def get_lyrics(project_id: UUID, user=Depends(get_current_user)):
    repo = MusicProjectsRepo()
    await _assert_owner(repo, project_id, user.id)

    row = await repo.get_lyrics(project_id=project_id)
    if not row:
        raise HTTPException(status_code=404, detail="lyrics_not_found")
    return MusicLyricsOut(**row)


@router.post("/{project_id}/lyrics", response_model=MusicLyricsOut)
async def upsert_lyrics(project_id: UUID, payload: UpsertMusicLyricsIn, user=Depends(get_current_user)):
    repo = MusicProjectsRepo()
    await _assert_owner(repo, project_id, user.id)

    await repo.upsert_lyrics(project_id=project_id, lyrics_text=payload.lyrics_text)

    row = await repo.get_lyrics(project_id=project_id)
    if not row:
        raise HTTPException(status_code=500, detail="lyrics_upsert_failed")
    return MusicLyricsOut(**row)


@router.get("/{project_id}/voice-reference", response_model=VoiceReferenceOut)
async def get_voice_reference_for_project(
    project_id: UUID,
    user=Depends(get_current_user),
):
    proj_repo = MusicProjectsRepo()
    row = await proj_repo.get(project_id=project_id, user_id=user.id)
    if not row:
        raise HTTPException(status_code=404, detail="project_not_found")

    voice_ref_asset_id = row.get("voice_ref_asset_id")
    if not voice_ref_asset_id:
        raise HTTPException(status_code=404, detail="voice_reference_not_found")

    pool = await get_pool()
    asset = await pool.fetchrow(
        """
        select id, content_type, bytes, storage_ref, meta_json, created_at
        from public.media_assets
        where id=$1 and user_id=$2
        limit 1
        """,
        voice_ref_asset_id,
        user.id,
    )
    if not asset:
        raise HTTPException(status_code=404, detail="voice_reference_not_found")

    asset_id = UUID(str(asset["id"]))
    ct = asset["content_type"]
    b = asset["bytes"]
    storage_ref = str(asset["storage_ref"]) if asset["storage_ref"] else None
    created_at = asset["created_at"]

    # ✅ HARD RULE: GET is read-only. No DB writes.
    readonly_ref = _get_asset_sas_readonly(storage_ref=storage_ref, meta_json=asset.get("meta_json"))

    return VoiceReferenceOut(
        project_id=project_id,
        voice_ref_asset_id=asset_id,
        content_type=str(ct or "audio/mpeg"),
        bytes=int(b or 0),
        storage_ref=str(readonly_ref or storage_ref or ""),
        created_at=created_at,
    )