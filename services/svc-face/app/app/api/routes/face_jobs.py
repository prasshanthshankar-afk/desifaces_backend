from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ValidationError

try:
    from desifaces_shared.pricing.client import PricingClientError
except Exception:
    class PricingClientError(Exception):
        """Fallback so svc-face can still boot if shared pricing package
        is missing from the container image."""
        pass

from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.blob import BlobSasPermissions, generate_blob_sas

from app.api.deps import get_current_user_id
from app.config import settings
from app.db import get_pool
from app.domain.models import (
    ContextConfigView,
    CreatorGenerateRequest,
    CreatorPlatformRequest,
    FaceGenerateRequest,
    FaceJobView,
    FaceProfileView,
    JobCreatedResponse,
    JobStatusResponse,
    PricingConfirmationModel,
    PricingPreviewRequestModel,
    PricingPreviewResponseModel,
    RegionConfigView,
)
from app.repos.creator_config_repo import CreatorPlatformConfigRepo
from app.repos.face_jobs_repo import FaceJobsRepo
from app.repos.face_profiles_repo import FaceProfilesRepo
from app.repos.media_assets_repo import MediaAssetsRepo
from app.services.creator_orchestrator import CreatorOrchestrator

router = APIRouter()
logger = logging.getLogger("api.face_jobs")

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
UPLOAD_CONTAINER = getattr(settings, "FACE_OUTPUT_CONTAINER", "face-output")
UPLOAD_PREFIX = "face-input"


def _parse_unsafe_prompt_reason(err: Exception) -> Optional[str]:
    """
    Extract a human-readable reason from exceptions thrown by prompt validation.
    Example:
      ValueError("unsafe_prompt: Blocked keyword detected: naked")
    """
    msg = str(err or "").strip()
    if not msg:
        return None
    if msg.startswith("unsafe_prompt:"):
        return msg.split("unsafe_prompt:", 1)[1].strip() or "unsafe_prompt"
    return None


def _raise_friendly_unsafe_prompt(user_id: str, reason: str) -> None:
    logger.info("Blocked unsafe prompt user_id=%s reason=%s", user_id, reason)
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


def _localized_text(value: Any, language: str, fallback: str) -> str:
    if isinstance(value, dict):
        return value.get(language) or value.get("en") or fallback
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _normalize_creator_generate_payload(
    payload: Dict[str, Any],
) -> tuple[CreatorPlatformRequest, Optional[PricingConfirmationModel]]:
    """
    Deterministic request parsing for creator generate.

    Why this exists:
    FastAPI/Pydantic union parsing can select the legacy CreatorPlatformRequest
    and silently drop pricing_confirmation because that model is permissive.
    So we parse the raw dict ourselves.

    Supported request shapes:
      - Legacy:
          { ...CreatorPlatformRequest fields... }

      - New:
          {
            "studio": "face",
            "studio_input": { ...CreatorPlatformRequest fields... },
            "pricing_confirmation": { ... }
          }
    """
    if not isinstance(payload, dict):
        raise ValueError("invalid_request_body")

    if "studio_input" in payload:
        wrapped = CreatorGenerateRequest.model_validate(payload)
        return wrapped.studio_input, wrapped.pricing_confirmation

    legacy = CreatorPlatformRequest.model_validate(payload)
    return legacy, None


class UploadImageResponse(BaseModel):
    asset_id: str
    image_url: str
    content_type: str
    size_bytes: int
    storage_path: str


def _azure_clients() -> tuple[BlobServiceClient, str, str, str]:
    """
    Returns:
      (blob_service_client, account_name, account_key, endpoint_suffix)
    from AZURE_STORAGE_CONNECTION_STRING.
    """
    conn = settings.AZURE_STORAGE_CONNECTION_STRING
    if not conn:
        raise RuntimeError("azure_storage_connection_string_missing")

    bsc = BlobServiceClient.from_connection_string(conn)

    parts: dict[str, str] = {}
    for chunk in conn.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()

    account_name = parts.get("AccountName") or ""
    account_key = parts.get("AccountKey") or ""
    endpoint_suffix = parts.get("EndpointSuffix") or "core.windows.net"

    if not account_name or not account_key:
        raise RuntimeError("azure_conn_string_missing_account_name_or_key")

    return bsc, account_name, account_key, endpoint_suffix


def _make_blob_url(
    account_name: str,
    endpoint_suffix: str,
    *,
    container: str,
    blob_name: str,
) -> str:
    quoted_blob = quote(blob_name, safe="/")
    return f"https://{account_name}.blob.{endpoint_suffix}/{container}/{quoted_blob}"


def _make_read_sas_url(
    account_name: str,
    account_key: str,
    endpoint_suffix: str,
    *,
    container: str,
    blob_name: str,
    hours: int = 24,
) -> str:
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
    )
    base_url = _make_blob_url(
        account_name,
        endpoint_suffix,
        container=container,
        blob_name=blob_name,
    )
    return f"{base_url}?{sas}"


# ------------------------------------------------------------------------------
# Upload (NEW) — for image-to-image identity lock
# ------------------------------------------------------------------------------

@router.post("/assets/upload", response_model=UploadImageResponse)
async def upload_source_image(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
) -> UploadImageResponse:
    """
    Upload a source image and create a media_assets row.

    Client usage:
      1) POST /api/face/assets/upload (multipart)
      2) Use returned asset_id in CreatorPlatformRequest:
         {
           "mode": "image-to-image",
           "source_image_asset_id": "<asset_id>",
           "preservation_strength": 0.22
         }
    """
    if not file:
        raise HTTPException(status_code=400, detail="missing_file")

    content_type = (file.content_type or "").strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=415,
            detail=f"unsupported_content_type:{content_type or 'unknown'}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty_file")

    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file_too_large:max={MAX_UPLOAD_BYTES}",
        )

    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
        if len(ext) > 8:
            ext = ""

    blob_name = f"{UPLOAD_PREFIX}/{user_id}/{uuid4().hex}{ext}"
    storage_path = f"az://{UPLOAD_CONTAINER}/{blob_name}"

    try:
        bsc, account_name, account_key, endpoint_suffix = _azure_clients()
        container_client = bsc.get_container_client(UPLOAD_CONTAINER)
        blob_client = container_client.get_blob_client(blob_name)

        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

        stable_blob_url = _make_blob_url(
            account_name,
            endpoint_suffix,
            container=UPLOAD_CONTAINER,
            blob_name=blob_name,
        )
        image_url = _make_read_sas_url(
            account_name,
            account_key,
            endpoint_suffix,
            container=UPLOAD_CONTAINER,
            blob_name=blob_name,
            hours=24,
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "upload_source_image azure upload failed user_id=%s filename=%s",
            user_id,
            getattr(file, "filename", None),
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "azure_upload_failed",
                "code": "DF_AZURE_UPLOAD_FAILED",
                "message": "Failed to upload source image.",
            },
        )

    pool = await get_pool()
    assets_repo = MediaAssetsRepo(pool)

    asset_id = await assets_repo.create_asset(
        user_id=str(user_id),
        kind="source_image",
        storage_ref=stable_blob_url,
        content_type=content_type,
        size_bytes=int(len(data)),
        meta={
            "purpose": "face_i2i_source",
            "filename": file.filename,
            "storage_container": UPLOAD_CONTAINER,
            "blob_name": blob_name,
            "storage_path": storage_path,
            "stable_blob_url": stable_blob_url,
            "uploaded_via": "api.face.assets.upload",
        },
    )

    return UploadImageResponse(
        asset_id=str(asset_id),
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
    user_id: str = Depends(get_current_user_id),
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
            generation_params=f.get("meta_json") or {},
        )
        for f in face_records
    ]

    return FaceJobView(
        job_id=str(job["id"]),
        status=str(job["status"]),
        faces=faces,
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
    )


@router.get("/jobs", response_model=List[FaceJobView])
async def list_legacy_user_jobs(
    user_id: str = Depends(get_current_user_id),
    limit: int = 20,
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
# CREATOR PLATFORM (LIVE) - wired to CreatorOrchestrator
# ------------------------------------------------------------------------------

@router.post("/creator/pricing/preview", response_model=PricingPreviewResponseModel)
async def creator_preview_pricing(
    req: PricingPreviewRequestModel,
    user_id: str = Depends(get_current_user_id),
) -> PricingPreviewResponseModel:
    """
    Creator platform pricing preview.

    Standardized flow:
      1) frontend sends studio_input here
      2) backend returns quote_id + preview_fingerprint + estimate/balance
      3) frontend confirms and then calls /creator/generate
    """
    pool = await get_pool()
    orch = CreatorOrchestrator(pool)

    try:
        return await orch.preview_pricing(
            user_id=user_id,
            request=req.studio_input,
            client_context=req.client_context,
        )

    except PricingClientError as e:
        logger.warning(
            "creator_preview_pricing pricing preview failed user_id=%s err=%s",
            user_id,
            str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "pricing_preview_failed",
                "code": "DF_PRICING_PREVIEW_FAILED",
                "message": str(e),
            },
        )

    except ValueError as e:
        reason = _parse_unsafe_prompt_reason(e)
        if reason:
            _raise_friendly_unsafe_prompt(user_id=str(user_id), reason=reason)

        logger.warning("creator_preview_pricing ValueError user_id=%s err=%s", user_id, str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "bad_request", "message": str(e)},
        )

    except HTTPException:
        raise

    except Exception:
        logger.exception("creator_preview_pricing failed user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "internal_error",
                "code": "DF_FACE_PRICING_PREVIEW_FAILED",
                "message": "Failed to preview pricing.",
            },
        )


@router.post("/creator/generate", response_model=JobCreatedResponse)
async def creator_generate_faces(
    payload: Dict[str, Any] = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> JobCreatedResponse:
    """
    Creator platform: database-driven diversity engine.
    Creates a job and returns creator job metadata.

    Supported request shapes:
      - Legacy: CreatorPlatformRequest
      - New: CreatorGenerateRequest { studio_input, pricing_confirmation }

    For image-to-image identity lock:
      - mode must be "image-to-image"
      - source_image_url or source_image_asset_id must be provided
      - preservation_strength ~ 0.15 - 0.35
    """
    pool = await get_pool()
    orch = CreatorOrchestrator(pool)

    try:
        studio_req, pricing_confirmation = _normalize_creator_generate_payload(payload)

        return await orch.create_job(
            user_id=user_id,
            request=studio_req,
            pricing_confirmation=pricing_confirmation,
        )

    except ValidationError as e:
        logger.warning("creator_generate_faces ValidationError user_id=%s err=%s", user_id, str(e))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "validation_error",
                "message": "Invalid creator generate request payload.",
                "fields": e.errors(),
            },
        )

    except PricingClientError as e:
        logger.warning(
            "creator_generate_faces pricing reservation failed user_id=%s err=%s",
            user_id,
            str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "pricing_reservation_failed",
                "code": "DF_PRICING_RESERVATION_FAILED",
                "message": str(e),
            },
        )

    except ValueError as e:
        reason = _parse_unsafe_prompt_reason(e)
        if reason:
            _raise_friendly_unsafe_prompt(user_id=str(user_id), reason=reason)

        logger.warning("creator_generate_faces ValueError user_id=%s err=%s", user_id, str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "bad_request", "message": str(e)},
        )

    except HTTPException:
        raise

    except Exception:
        logger.exception("creator_generate_faces failed user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "internal_error",
                "code": "DF_FACE_CREATE_FAILED",
                "message": "Failed to create face job.",
            },
        )


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
        meta = _get(j, "meta_json", {}) or {}
        if isinstance(meta, str):
            meta = {}
        if meta.get("request_type") == "creator_platform":
            creator_job_ids.append(str(_get(j, "id")))

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
    limit: int = 50,
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
            generation_params=p.get("meta_json") or {},
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
                display_name=_localized_text(
                    r.get("display_name"),
                    language,
                    r["code"],
                ),
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
            display_name=_localized_text(
                r.get("display_name"),
                language,
                r["code"],
            ),
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
    SELECT code, display_name, prompt_base, is_active, meta_json
    FROM public.face_generation_contexts
    WHERE is_active = TRUE
    ORDER BY sort_order NULLS LAST, code
    """
    rows = await config_repo.execute_queries(q)
    rows = [config_repo.convert_db_row(r) for r in rows]

    result: List[ContextConfigView] = []
    for r in rows:
        display_name = _localized_text(
            r.get("display_name"),
            "en",
            r["code"].replace("_", " ").title(),
        )

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