from __future__ import annotations

from uuid import UUID
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.repos.artifacts_repo import ArtifactsRepo
from app.repos.media_assets_repo import MediaAssetsRepo
from app.services.azure_storage_service import AzureStorageService


router = APIRouter(prefix="/music/assets", tags=["music_assets"])

artifacts = ArtifactsRepo()
media_assets = MediaAssetsRepo()
storage = AzureStorageService()


class AssetUploadResponse(BaseModel):
    artifact_id: str
    media_asset_id: str | None = None
    storage_path: str
    sas_url: str
    content_type: str
    bytes: int
    sha256: str


class AssetGetResponse(BaseModel):
    artifact_id: str
    storage_path: str
    sas_url: str
    content_type: str
    bytes: int
    sha256: str
    kind: str
    project_id: str | None = None
    job_id: str | None = None
    # We can't resolve this from artifact_id unless you store a mapping, so it's optional.
    media_asset_id: str | None = None


_AUDIO_KIND_HINTS = {
    "byo_audio",
    "audio_master",
    "song_audio",
    "music_audio",
    "full_mix",
    "voice_reference",
    "voice_ref",
}


def _looks_like_audio(kind: str, content_type: str) -> bool:
    k = (kind or "").strip().lower()
    ct = (content_type or "").strip().lower()
    if ct.startswith("audio/"):
        return True
    if k in _AUDIO_KIND_HINTS:
        return True
    return False


@router.post("/upload", response_model=AssetUploadResponse)
async def upload_music_asset(
    kind: str,
    file: UploadFile = File(...),
    project_id: str | None = None,
    job_id: str | None = None,
    duration_ms: int | None = None,
    user_id: UUID = Depends(get_current_user_id),
):
    kind = (kind or "").strip()
    if not kind:
        raise HTTPException(status_code=400, detail="missing_kind")

    # Validate optional ids early (prevents 500s on UUID() casts)
    pid_uuid: UUID | None = None
    jid_uuid: UUID | None = None
    if project_id:
        try:
            pid_uuid = UUID(str(project_id))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_project_id")
    if job_id:
        try:
            jid_uuid = UUID(str(job_id))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_job_id")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty_file")

    filename = (file.filename or "").strip()
    ext = "bin"
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower() or "bin"

    content_type = (file.content_type or "application/octet-stream").strip()

    # upload to blob
    up = await storage.upload_bytes(
        data=data,
        user_id=str(user_id),
        scope_id=str(project_id or job_id or "music"),
        variant=1,
        ext=ext,
        content_type=content_type,
    )

    # create artifact row (existing behavior)
    aid = await artifacts.create(
        user_id=user_id,
        project_id=pid_uuid,
        job_id=jid_uuid,
        kind=kind,
        storage_path=up.storage_path,
        content_type=content_type,
        bytes=up.bytes,
        sha256=up.sha256,
    )

    # NEW: create a media_assets row for audio (so you can refresh SAS later like voice_ref)
    media_asset_id: UUID | None = None
    if _looks_like_audio(kind, content_type):
        meta_json = {
            # Keep canonical blob identity for SAS refresh:
            # - storage_path is what your AzureStorageService.sas_url_for expects
            "storage_path": up.storage_path,
            "original_filename": filename or None,
            "project_id": str(pid_uuid) if pid_uuid else None,
            "job_id": str(jid_uuid) if jid_uuid else None,
            "source": "music_assets_upload",
        }

        try:
            media_asset_id, _created_at = await media_assets.create_asset(
                user_id=user_id,
                kind=kind,
                storage_ref=up.sas_url,  # store the SAS URL as storage_ref (your orchestrator expects this pattern)
                content_type=content_type,
                bytes_len=up.bytes,
                sha256_hex=up.sha256,
                duration_ms=int(duration_ms) if duration_ms is not None else None,
                meta_json=meta_json,
            )
        except Exception:
            # Don't break uploads if media_assets insert fails; caller still gets artifact_id + sas_url
            media_asset_id = None

    return AssetUploadResponse(
        artifact_id=str(aid),
        media_asset_id=str(media_asset_id) if media_asset_id else None,
        storage_path=up.storage_path,
        sas_url=up.sas_url,
        content_type=content_type,
        bytes=up.bytes,
        sha256=up.sha256,
    )


@router.get("/{artifact_id}", response_model=AssetGetResponse)
async def get_music_asset(
    artifact_id: str,
    user_id: UUID = Depends(get_current_user_id),
):
    try:
        aid = UUID(artifact_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_artifact_id")

    row = await artifacts.get(aid)
    if not row:
        raise HTTPException(status_code=404, detail="artifact_not_found")

    if str(row.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="forbidden")

    sas = storage.sas_url_for(row["storage_path"])

    return AssetGetResponse(
        artifact_id=str(row["id"]),
        storage_path=row["storage_path"],
        sas_url=sas,
        content_type=row["content_type"],
        bytes=row["bytes"],
        sha256=row["sha256"],
        kind=row["kind"],
        project_id=str(row["project_id"]) if row.get("project_id") else None,
        job_id=str(row["job_id"]) if row.get("job_id") else None,
        media_asset_id=None,
    )