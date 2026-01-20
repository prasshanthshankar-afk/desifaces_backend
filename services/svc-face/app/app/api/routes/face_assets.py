from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.deps import get_current_user_id
from app.config import settings
from app.db import get_pool
from app.repos.media_assets_repo import MediaAssetsRepo

try:
    from azure.storage.blob import BlobServiceClient
    from azure.storage.blob import generate_blob_sas, BlobSasPermissions
except Exception:  # pragma: no cover
    BlobServiceClient = None  # type: ignore
    generate_blob_sas = None  # type: ignore
    BlobSasPermissions = None  # type: ignore


router = APIRouter()

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _parse_azure_conn_str(conn_str: str) -> Dict[str, str]:
    """
    Parse Azure Storage connection string into dict.
    Expected keys include: AccountName, AccountKey, EndpointSuffix, DefaultEndpointsProtocol
    """
    out: Dict[str, str] = {}
    for part in (conn_str or "").split(";"):
        if not part.strip():
            continue
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _is_allowed_image(content_type: Optional[str]) -> bool:
    ct = (content_type or "").lower().strip()
    # Keep this strict for safety + predictable downstream behavior
    return ct in ("image/jpeg", "image/jpg", "image/png", "image/webp")


def _safe_ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").lower().strip()
    if ct in ("image/jpeg", "image/jpg"):
        return "jpg"
    if ct == "image/png":
        return "png"
    if ct == "image/webp":
        return "webp"
    return "bin"


# ------------------------------------------------------------------------------
# Upload endpoint (Option A: store in Azure + return SAS URL)
# ------------------------------------------------------------------------------

@router.post("/assets/upload")
async def upload_face_source_image(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Upload a user-provided source image for image-to-image workflows (identity lock).
    Option A (production): store in Azure Blob and return a SAS URL + media_asset_id.

    Route (when mounted under /api/face): POST /api/face/assets/upload
    """
    if BlobServiceClient is None or generate_blob_sas is None:
        raise HTTPException(
            status_code=500,
            detail="azure_storage_blob_sdk_missing: install azure-storage-blob in svc-face image",
        )

    content_type = (file.content_type or "").strip()
    if not _is_allowed_image(content_type):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported_content_type: {content_type or '<empty>'} (allowed: image/jpeg, image/png, image/webp)",
        )

    # Read bytes
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty_file")

    # Safety guard (tune later)
    max_bytes = int(os.getenv("DF_MAX_UPLOAD_BYTES", "10485760"))  # 10MB default
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file_too_large: {len(data)} bytes (max {max_bytes})",
        )

    conn_str = settings.AZURE_STORAGE_CONNECTION_STRING
    if not conn_str:
        raise HTTPException(status_code=500, detail="missing_azure_storage_connection_string")

    # Container for *inputs* (separate from face-output)
    # If you don’t set it, we default to "face-input".
    container = os.getenv("FACE_INPUT_CONTAINER", "face-input").strip() or "face-input"

    # Create a stable blob name
    ext = _safe_ext_from_content_type(content_type)
    # keep original filename only for human debugging; never trust it for path safety
    original_name = (file.filename or "").strip().replace("/", "_").replace("\\", "_")
    original_name = original_name[:80] if original_name else f"upload.{ext}"

    blob_name = f"{user_id}/uploads/{uuid4().hex}.{ext}"

    # Upload to blob
    try:
        bsc = BlobServiceClient.from_connection_string(conn_str)
        cc = bsc.get_container_client(container)
        try:
            # idempotent-ish create
            cc.create_container()
        except Exception:
            pass

        bc = cc.get_blob_client(blob_name)
        bc.upload_blob(
            data,
            overwrite=True,
            content_type=content_type,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"azure_upload_failed: {e}") from e

    # Build SAS
    parts = _parse_azure_conn_str(conn_str)
    account_name = parts.get("AccountName") or ""
    account_key = parts.get("AccountKey") or ""
    if not account_name or not account_key:
        raise HTTPException(status_code=500, detail="azure_conn_str_missing_account_fields")

    expiry_hours = int(os.getenv("DF_UPLOAD_SAS_EXPIRES_HOURS", "24"))
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)

    try:
        sas = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=expires_at,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"sas_generation_failed: {e}") from e

    # This is the SAS URL callers can feed into i2i
    sas_url = f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"

    # Persist a media_asset row (so everything is auditable + re-usable)
    pool = await get_pool()
    assets_repo = MediaAssetsRepo(pool)

    meta: Dict[str, Any] = {
        "purpose": "face_i2i_source",
        "original_filename": original_name,
        "uploaded_content_type": content_type,
        "container": container,
        "blob_name": blob_name,
        "sas_expires_at": expires_at.isoformat(),
    }

    try:
        media_asset_id = await assets_repo.create_asset(
            user_id=user_id,
            kind="face_source_image",
            storage_ref=sas_url,  # store the SAS for now; alternatively store non-SAS + generate on demand
            content_type=content_type,
            size_bytes=len(data),
            meta=meta,
        )
    except Exception as e:
        # Upload succeeded; DB failed -> still return SAS so user isn’t blocked
        # but signal partial failure clearly.
        raise HTTPException(status_code=500, detail=f"db_create_media_asset_failed: {e}") from e

    return {
        "media_asset_id": media_asset_id,
        "content_type": content_type,
        "bytes": len(data),
        "container": container,
        "blob_name": blob_name,
        "source_image_url": sas_url,
        "expires_at": expires_at.isoformat(),
    }