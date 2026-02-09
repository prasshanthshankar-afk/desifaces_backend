from __future__ import annotations

from typing import Any, List, Optional
from uuid import uuid4
from datetime import datetime, timedelta, timezone
import logging
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.db import get_pool



from app.domain.models import (
    # Legacy models
    FaceGenerateRequest,
    FaceJobView,
    FaceProfileView,
    RegionConfigView,
    ContextConfigView,
    # Creator platform models
    CreatorPlatformRequest,
    JobCreatedResponse,
    JobStatusResponse,
)

from app.services.creator_orchestrator import CreatorOrchestrator

from app.repos.face_jobs_repo import FaceJobsRepo
from app.repos.face_profiles_repo import FaceProfilesRepo
from app.repos.creator_config_repo import CreatorPlatformConfigRepo
from app.repos.media_assets_repo import MediaAssetsRepo

from app.config import settings

# Azure blob (used for upload endpoint)
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.blob import generate_blob_sas, BlobSasPermissions

router = APIRouter()

logger = logging.getLogger("api.face_jobs")



# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# Friendly error mapping (prompt safety)
# ------------------------------------------------------------------------------

def _parse_unsafe_prompt_reason(err: Exception) -> Optional[str]:
    """
    Extracts a human-readable reason from exceptions thrown by prompt validation,
    e.g. ValueError("unsafe_prompt: Blocked keyword detected: naked")
    """
    msg = str(err or "").strip()
    if not msg:
        return None

    # Current convention in CreatorPromptService:
    # raise ValueError(f"unsafe_prompt: {reason}")
    if msg.startswith("unsafe_prompt:"):
        return msg.split("unsafe_prompt:", 1)[1].strip() or "unsafe_prompt"

    return None


def _raise_friendly_unsafe_prompt(user_id: str, reason: str) -> None:
    """
    Raise a clean 400 error payload the client can show nicely.
    We keep the reason (for debugging) but provide a friendly message.
    """
    logger.info("Blocked unsafe prompt user_id=%s reason=%s", user_id, reason)

    # Friendly message shown to end user (safe wording)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "unsafe_prompt",
            "code": "DF_UNSAFE_PROMPT",
            "message": (
                "That prompt isn’t allowed. Please remove sexual or explicit content "
                "and try again."
            ),
            "reason": reason,
            "action": "edit_prompt",
        },
    )

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Safe getter for dicts, pydantic models, and asyncpg Records."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, key):
        return getattr(obj, key, default)
    try:
        return obj[key]
    except Exception:
        return default

def _assert_owner(job: Any, user_id: str) -> None:
    job_user_id = str(_get(job, "user_id", ""))
    if job_user_id != str(user_id):
        raise HTTPException(status_code=403, detail="forbidden")

# ------------------------------------------------------------------------------
# Upload (NEW) — for image-to-image identity lock
# ------------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB (tune later)
UPLOAD_CONTAINER = getattr(settings, "FACE_OUTPUT_CONTAINER", "face-output")  # reuse for now
UPLOAD_PREFIX = "face-input"  # logical folder within container

class UploadImageResponse(BaseModel):
    asset_id: str
    image_url: str
    content_type: str
    size_bytes: int
    storage_path: str

def _azure_clients() -> tuple[BlobServiceClient, str, str]:
    """
    Returns (bsc, account_name, account_key) from AZURE_STORAGE_CONNECTION_STRING.
    This enables both upload and SAS generation.
    """
    conn = settings.AZURE_STORAGE_CONNECTION_STRING
    bsc = BlobServiceClient.from_connection_string(conn)

    # Parse conn string for AccountName/AccountKey (needed for SAS generation)
    # Example: "AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
    parts = {}
    for chunk in conn.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    account_name = parts.get("AccountName") or ""
    account_key = parts.get("AccountKey") or ""
    if not account_name or not account_key:
        raise RuntimeError("azure_conn_string_missing_account_name_or_key")
    return bsc, account_name, account_key

def _make_read_sas_url(account_name: str, account_key: str, *, container: str, blob_name: str, hours: int = 24) -> str:
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
    )
    return f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"

@router.post("/assets/upload", response_model=UploadImageResponse)
async def upload_source_image(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
) -> UploadImageResponse:
    """
    Upload a source image (phone/desktop) and create a media_assets row.

    Client usage:
      1) POST /api/face/assets/upload (multipart)
      2) Use returned asset_id in CreatorPlatformRequest:
         { mode: "image-to-image", source_image_asset_id: "<asset_id>", preservation_strength: 0.22, ... }
    """
    if not file:
        raise HTTPException(status_code=400, detail="missing_file")

    content_type = (file.content_type or "").strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail=f"unsupported_content_type:{content_type or 'unknown'}")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty_file")

    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"file_too_large:max={MAX_UPLOAD_BYTES}")

    # Create blob path
    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
        if len(ext) > 8:
            ext = ""
    blob_name = f"{UPLOAD_PREFIX}/{user_id}/{uuid4().hex}{ext}"
    storage_path = f"{UPLOAD_CONTAINER}/{blob_name}"

    try:
        bsc, account_name, account_key = _azure_clients()
        container_client = bsc.get_container_client(UPLOAD_CONTAINER)
        # Container should already exist in your infra; if not, this will throw.
        blob_client = container_client.get_blob_client(blob_name)

        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

        # Return a read SAS URL (24h)
        image_url = _make_read_sas_url(account_name, account_key, container=UPLOAD_CONTAINER, blob_name=blob_name, hours=24)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"azure_upload_failed:{e}") from e

    # Persist as media asset
    pool = await get_pool()
    assets_repo = MediaAssetsRepo(pool)

    asset_id = await assets_repo.create_asset(
        user_id=str(user_id),
        kind="source_image",
        storage_ref=image_url,
        content_type=content_type,
        size_bytes=int(len(data)),
        meta={
            "purpose": "face_i2i_source",
            "filename": file.filename,
            "storage_container": UPLOAD_CONTAINER,
            "blob_name": blob_name,
            "storage_path": storage_path,
        },
    )

    return UploadImageResponse(
        asset_id=asset_id,
        image_url=image_url,
        content_type=content_type,
        size_bytes=int(len(data)),
        storage_path=storage_path,
    )

# ------------------------------------------------------------------------------
# LEGACY FACE GENERATION (backward compatible)
# ------------------------------------------------------------------------------

@router.post("/generate", response_model=FaceJobView)
async def generate_faces(
    req: FaceGenerateRequest,
    user_id: str = Depends(get_current_user_id),
) -> FaceJobView:
    """
    Legacy: Generate face images using prompt-based system.
    Returns job_id - use /jobs/{job_id} to check status.
    """
    pool = await get_pool()

    # Import lazily so service startup doesn’t fail if legacy orchestrator is removed.
    try:
        from app.services.face_orchestrator import FaceOrchestrator  # type: ignore
    except Exception:
        raise HTTPException(status_code=501, detail="legacy_orchestrator_not_available")

    orch = FaceOrchestrator(pool)
    job_id = await orch.create_job(user_id=user_id, req=req)

    return FaceJobView(job_id=job_id, status="queued", faces=[])

@router.get("/jobs/{job_id}", response_model=FaceJobView)
async def get_legacy_job_status(
    job_id: str,
    user_id: str = Depends(get_current_user_id)
) -> FaceJobView:
    """
    Legacy: Get face generation job status and results.
    """
    pool = await get_pool()
    jobs_repo = FaceJobsRepo(pool)
    profiles_repo = FaceProfilesRepo(pool)

    job = await jobs_repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    _assert_owner(job, user_id)

    face_records = await profiles_repo.get_job_faces(job_id)

    faces = [
        FaceProfileView(
            face_profile_id=str(f["id"]),
            image_url=str(f["image_url"]) if f.get("image_url") else "",
            thumbnail_url=None,
            variant=(f.get("attributes_json") or {}).get("variant", 0),
            generation_params=f.get("meta_json") or {}
        )
        for f in face_records
    ]

    return FaceJobView(
        job_id=str(job["id"]),
        status=str(job["status"]),
        faces=faces,
        error_code=job.get("error_code"),
        error_message=job.get("error_message")
    )

@router.get("/jobs", response_model=List[FaceJobView])
async def list_legacy_user_jobs(
    user_id: str = Depends(get_current_user_id),
    limit: int = 20
) -> List[FaceJobView]:
    """
    Legacy: List user's face generation jobs.
    """
    pool = await get_pool()
    jobs_repo = FaceJobsRepo(pool)

    jobs = await jobs_repo.list_user_jobs(user_id, limit)

    return [
        FaceJobView(job_id=str(j["id"]), status=str(j["status"]), faces=[])
        for j in jobs
    ]

# ------------------------------------------------------------------------------
# CREATOR PLATFORM (NEW) - wired to CreatorOrchestrator
# ------------------------------------------------------------------------------

@router.post("/creator/generate", response_model=JobCreatedResponse)
async def creator_generate_faces(
    req: CreatorPlatformRequest,
    user_id: str = Depends(get_current_user_id),
) -> JobCreatedResponse:
    """
    Creator platform: database-driven diversity engine.
    Creates a job and returns creator job metadata.

    For image-to-image identity lock:
      - req.mode must be "image-to-image"
      - req.source_image_url must be provided
      - req.preservation_strength ~ 0.15 - 0.35 (DesiFaces semantics)
    """
    pool = await get_pool()
    orch = CreatorOrchestrator(pool)

    try:
        return await orch.create_job(user_id=user_id, request=req)
    except ValueError as e:
        reason = _parse_unsafe_prompt_reason(e)
        if reason:
            _raise_friendly_unsafe_prompt(user_id=str(user_id), reason=reason)
        # Not an unsafe prompt; treat as a normal validation error
        logger.warning("creator_generate_faces ValueError user_id=%s err=%s", user_id, str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "bad_request", "message": str(e)},
        )
    except HTTPException:
        # Preserve explicit HTTP errors
        raise
    

@router.get("/creator/jobs/{job_id}/status", response_model=JobStatusResponse)
async def creator_get_job_status(
    job_id: str,
    user_id: str = Depends(get_current_user_id),
) -> JobStatusResponse:
    """
    Creator platform: Get job status + variants.
    Uses CreatorOrchestrator.get_job_status() which reads from face_job_outputs + artifacts.
    """
    pool = await get_pool()
    jobs_repo = FaceJobsRepo(pool)

    job = await jobs_repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    _assert_owner(job, user_id)

    orch = CreatorOrchestrator(pool)
    return await orch.get_job_status(job_id)

@router.get("/creator/jobs", response_model=List[JobStatusResponse])
async def creator_list_jobs(
    user_id: str = Depends(get_current_user_id),
    limit: int = 20,
) -> List[JobStatusResponse]:
    """
    Creator platform: List user's creator jobs.
    Minimal implementation: return status objects for latest jobs.
    """
    pool = await get_pool()
    jobs_repo = FaceJobsRepo(pool)
    orch = CreatorOrchestrator(pool)

    jobs = await jobs_repo.list_user_jobs(user_id, limit)

    creator_job_ids: List[str] = []
    for j in jobs:
        meta = j.get("meta_json") or {}
        if meta.get("request_type") == "creator_platform":
            creator_job_ids.append(str(j["id"]))

    results: List[JobStatusResponse] = []
    for jid in creator_job_ids:
        results.append(await orch.get_job_status(jid))

    return results

# ------------------------------------------------------------------------------
# Shared endpoints (profiles + config)
# ------------------------------------------------------------------------------

@router.get("/profiles", response_model=List[FaceProfileView])
async def list_user_profiles(
    user_id: str = Depends(get_current_user_id),
    limit: int = 50
) -> List[FaceProfileView]:
    """
    List user's saved face profiles.
    """
    pool = await get_pool()
    profiles_repo = FaceProfilesRepo(pool)

    profiles = await profiles_repo.list_user_profiles(user_id, limit)

    return [
        FaceProfileView(
            face_profile_id=str(p["id"]),
            image_url=str(p["image_url"]) if p.get("image_url") else "",
            thumbnail_url=None,
            variant=(p.get("attributes_json") or {}).get("variant", 0),
            generation_params=p.get("meta_json") or {}
        )
        for p in profiles
    ]

@router.get("/config/regions", response_model=List[RegionConfigView])
async def get_available_regions(language: str = "en") -> List[RegionConfigView]:
    """
    Get available regions for face generation.
    Uses creator config repo (svc-face canonical config source).
    """
    pool = await get_pool()
    config_repo = CreatorPlatformConfigRepo(pool)

    if hasattr(config_repo, "list_regions"):
        regions = await config_repo.list_regions(active_only=True)
        return [
            RegionConfigView(
                code=r["code"],
                display_name=(r.get("display_name") or {}).get(language)
                or (r.get("display_name") or {}).get("en")
                or r["code"],
                sub_region=r.get("sub_region"),
                is_active=bool(r.get("is_active", True)),
            )
            for r in regions
        ]

    q = """
    SELECT code, display_name, sub_region, is_active
    FROM public.face_generation_regions
    WHERE ($1::bool IS FALSE OR is_active = TRUE)
    ORDER BY code
    """
    rows = await config_repo.execute_queries(q, True)
    rows = [config_repo.convert_db_row(r) for r in rows]

    return [
        RegionConfigView(
            code=r["code"],
            display_name=(r.get("display_name") or {}).get(language)
            or (r.get("display_name") or {}).get("en")
            or r["code"],
            sub_region=r.get("sub_region"),
            is_active=bool(r.get("is_active", True)),
        )
        for r in rows
    ]

@router.get("/config/contexts", response_model=List[ContextConfigView])
async def get_available_contexts() -> List[ContextConfigView]:
    """
    Get available contexts.
    Uses face_generation_contexts table.
    """
    pool = await get_pool()
    config_repo = CreatorPlatformConfigRepo(pool)

    q = """
    SELECT code, display_name, prompt_base, is_active
    FROM public.face_generation_contexts
    WHERE is_active = TRUE
    ORDER BY sort_order NULLS LAST, code
    """
    rows = await config_repo.execute_queries(q)
    rows = [config_repo.convert_db_row(r) for r in rows]

    result: List[ContextConfigView] = []
    for r in rows:
        dn = r.get("display_name")
        if isinstance(dn, dict):
            display_name = dn.get("en") or r["code"].replace("_", " ").title()
        else:
            display_name = dn or r["code"].replace("_", " ").title()

        result.append(
            ContextConfigView(
                code=r["code"],
                display_name=display_name,
                economic_class=(r.get("meta_json") or {}).get("economic_class", "unknown"),
                glamour_level=(r.get("meta_json") or {}).get("glamour_level", 0),
                is_active=bool(r.get("is_active", True)),
            )
        )
    return result