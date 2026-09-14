from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.db import get_pool
from app.repos.media_assets_repo import MediaAssetsRepo
from app.services.azure_storage_service import AzureStorageService


router = APIRouter()


class FaceAssetReadUrlResponse(BaseModel):
    media_id: str
    read_url: str
    image_url: str
    face_image_url: str
    source_image_url: str
    url: str
    content_type: Optional[str] = None


def _is_service_actor(actor_id: str) -> bool:
    return str(actor_id or "").strip().lower().startswith("svc-")


@router.get("/assets/{media_id}/read-url", response_model=FaceAssetReadUrlResponse)
async def get_face_asset_read_url(
    media_id: str,
    user_id: str = Depends(get_current_user_id),
) -> FaceAssetReadUrlResponse:
    """Return a fresh, read-only URL for a durable Face media asset.

    This is the canonical Face -> Director/Fusion handoff contract. Normal users
    may only read their own asset. Authenticated service tokens are allowed to
    resolve assets for asynchronous internal orchestration.
    """
    pool = await get_pool()
    repo = MediaAssetsRepo(pool)

    try:
        asset = await repo.get_asset(media_id)
    except Exception:
        asset = None

    if asset is None:
        raise HTTPException(status_code=404, detail="face_asset_not_found")

    actor_id = str(user_id or "")
    asset_user_id = str(getattr(asset, "user_id", "") or "")
    if not _is_service_actor(actor_id) and asset_user_id != actor_id:
        # Avoid exposing whether another user's asset exists.
        raise HTTPException(status_code=404, detail="face_asset_not_found")

    kind = str(getattr(asset, "kind", "") or "").strip().lower()
    if kind not in {"face_image", "face_source_image"}:
        raise HTTPException(status_code=404, detail="face_asset_not_found")

    storage_ref = str(getattr(asset, "storage_ref", "") or "").strip()
    meta_json = getattr(asset, "meta_json", None) or {}
    if not storage_ref and not meta_json:
        raise HTTPException(status_code=404, detail="face_asset_storage_missing")

    try:
        storage = AzureStorageService()
        read_url = await storage.get_readonly_sas_url(
            storage_ref=storage_ref or None,
            meta_json=meta_json,
            hours=24,
            refresh_if_within_minutes=60,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="face_asset_read_url_unavailable") from exc

    if not read_url:
        raise HTTPException(status_code=404, detail="face_asset_read_url_missing")

    content_type = str(getattr(asset, "content_type", "") or "").strip() or None
    media_id_value = str(getattr(asset, "id", media_id) or media_id)
    return FaceAssetReadUrlResponse(
        media_id=media_id_value,
        read_url=read_url,
        image_url=read_url,
        face_image_url=read_url,
        source_image_url=read_url,
        url=read_url,
        content_type=content_type,
    )
