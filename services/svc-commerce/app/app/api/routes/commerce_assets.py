from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from app.api.deps import require_user
from app.db import get_pool
from app.services.azure_storage_service import AzureStorageService

router = APIRouter(prefix="/api/commerce/assets", tags=["commerce"])

_ROLE_RE = re.compile(r"^[a-z0-9_]{2,64}$")


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _try_image_dims(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    try:
        from PIL import Image  # pillow is already in your training stack; if missing, this safely falls back

        im = Image.open(io.BytesIO(data))
        return int(im.width), int(im.height)
    except Exception:
        return None, None


def _guess_ext(filename: str, content_type: str) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".png"):
        return ".png"
    if fn.endswith(".jpg") or fn.endswith(".jpeg"):
        return ".jpg"
    if fn.endswith(".webp"):
        return ".webp"
    # fallback from content-type
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "webp" in ct:
        return ".webp"
    return ".bin"


def _parse_az_storage_ref(storage_ref: str) -> Tuple[str, str]:
    """
    storage_ref can be:
      - az://container/blob
      - container/blob
      - https://.../container/blob?... (AzureStorageService can parse, but we want container/blob for seconds-based SAS)
    """
    s = (storage_ref or "").strip()
    if s.startswith("az://"):
        rest = s[len("az://") :]
        c, b = rest.split("/", 1)
        return c, b.lstrip("/")
    if s.startswith("http://") or s.startswith("https://"):
        # Let AzureStorageService resolve if needed, but simplest: strip host and query
        from urllib.parse import urlsplit

        path = urlsplit(s.split("?", 1)[0]).path.lstrip("/")
        c, b = path.split("/", 1)
        return c, b.lstrip("/")
    if "/" in s:
        c, b = s.split("/", 1)
        return c.strip(), b.lstrip("/")
    raise ValueError(f"unsupported storage_ref: {storage_ref!r}")


@router.post("/upload", operation_id="commerce_assets_upload")
async def upload(
    role: str = Form(..., description="Asset role e.g. saree_full, pallu_full, border_closeup, worn_ref_front, person_full_body"),
    owner_type: str = Form("merchant", description="merchant|consumer|internal"),
    owner_id: Optional[UUID] = Form(None, description="Defaults to authenticated user_id for now"),
    file: UploadFile = File(...),
    user_id: UUID = Depends(require_user),
) -> Dict[str, Any]:
    role = (role or "").strip().lower()
    if not _ROLE_RE.match(role):
        raise HTTPException(status_code=422, detail="invalid_role (use lowercase letters/numbers/underscore, 2-64 chars)")

    owner_type = (owner_type or "merchant").strip().lower()
    if owner_type not in ("merchant", "consumer", "internal"):
        raise HTTPException(status_code=422, detail="invalid_owner_type")

    # For now: safest policy (same as your other flows): only allow uploading for yourself.
    # When you add vendor API keys, you can relax this.
    owner_id = owner_id or user_id
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="owner_id_must_equal_authenticated_user_for_now")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty_file")

    content_type = (file.content_type or "application/octet-stream").strip()
    digest = _sha256(data)
    width, height = _try_image_dims(data)

    pool = await get_pool()

    # Dedup: you already have uq_media_assets_user_sha256 (user_id, sha256) WHERE sha256 IS NOT NULL
    async with pool.acquire() as con:
        existing = await con.fetchrow(
            """
            select id, storage_ref, content_type, bytes, sha256, width, height, meta_json
            from public.media_assets
            where user_id=$1 and sha256=$2
            """,
            user_id,
            digest,
        )
        if existing:
            storage = AzureStorageService()
            # return a fresh signed URL (minutes-level using seconds-based signer)
            c, b = _parse_az_storage_ref(str(existing["storage_ref"]))
            url = storage.get_blob_sas_url(container=c, blob_name=b, expires_in_s=60 * 60, permission="r")
            meta = _as_dict(existing["meta_json"])
            return {
                "asset_id": str(existing["id"]),
                "deduped": True,
                "role": meta.get("role") or role,
                "storage_ref": str(existing["storage_ref"]),
                "sha256": str(existing["sha256"]),
                "width": existing["width"],
                "height": existing["height"],
                "content_type": existing["content_type"],
                "bytes": existing["bytes"],
                "preview_url": url,
            }

    storage = AzureStorageService()
    # Use a separate container if you want (optional env); otherwise uses COMMERCE_OUTPUT_CONTAINER
    container = (Path((storage.container or "").strip()).name) or storage.container
    container_override = (Path((__import__("os").environ.get("COMMERCE_ASSETS_CONTAINER") or "").strip()).name)
    if container_override:
        container = container_override

    ext = _guess_ext(file.filename or "", content_type)
    asset_id = uuid4()

    blob = f"commerce_assets/{user_id}/{role}/{asset_id}{ext}"
    # Upload via AzureStorageService (returns SAS url, but we store stable az:// ref)
    _ = storage.upload_bytes(
        data=data,
        blob_name=blob,
        content_type=content_type,
        overwrite=True,
        container_name=container,
    )

    storage_ref = f"az://{container}/{blob}"

    meta_json = {
        "role": role,
        "owner_type": owner_type,
        "owner_id": str(owner_id),
        "filename": file.filename,
        "source": "commerce_assets.upload",
    }

    async with pool.acquire() as con:
        try:
            await con.execute(
                """
                insert into public.media_assets(
                    id, user_id, kind, storage_ref, content_type, bytes, sha256, width, height, meta_json, created_at, updated_at
                )
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb, now(), now())
                """,
                asset_id,
                user_id,
                "commerce_person" if owner_type == "consumer" else "commerce_asset",
                storage_ref,
                content_type,
                len(data),
                digest,
                width,
                height,
                json.dumps(meta_json),
            )
        except Exception:
            # handle rare race where the unique(user_id,sha256) wins elsewhere
            row = await con.fetchrow(
                "select id, storage_ref, sha256, width, height, content_type, bytes, meta_json from public.media_assets where user_id=$1 and sha256=$2",
                user_id,
                digest,
            )
            if row:
                c, b = _parse_az_storage_ref(str(row["storage_ref"]))
                url = storage.get_blob_sas_url(container=c, blob_name=b, expires_in_s=60 * 60, permission="r")
                meta = _as_dict(row["meta_json"])
                return {
                    "asset_id": str(row["id"]),
                    "deduped": True,
                    "role": meta.get("role") or role,
                    "storage_ref": str(row["storage_ref"]),
                    "sha256": str(row["sha256"]),
                    "width": row["width"],
                    "height": row["height"],
                    "content_type": row["content_type"],
                    "bytes": row["bytes"],
                    "preview_url": url,
                }
            raise

    # fresh preview URL (1 hour)
    preview_url = storage.get_blob_sas_url(container=container, blob_name=blob, expires_in_s=60 * 60, permission="r")
    return {
        "asset_id": str(asset_id),
        "deduped": False,
        "role": role,
        "storage_ref": storage_ref,
        "sha256": digest,
        "width": width,
        "height": height,
        "content_type": content_type,
        "bytes": len(data),
        "preview_url": preview_url,
    }


@router.get("/{asset_id}", operation_id="commerce_assets_get")
async def get_asset(asset_id: UUID, user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            select id, user_id, kind, storage_ref, content_type, bytes, sha256, width, height, meta_json, created_at, updated_at
            from public.media_assets
            where id=$1
            """,
            asset_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="asset_not_found")
    if UUID(str(row["user_id"])) != user_id:
        raise HTTPException(status_code=403, detail="forbidden")

    return {
        "asset_id": str(row["id"]),
        "user_id": str(row["user_id"]),
        "kind": row["kind"],
        "storage_ref": row["storage_ref"],
        "content_type": row["content_type"],
        "bytes": row["bytes"],
        "sha256": row["sha256"],
        "width": row["width"],
        "height": row["height"],
        "meta_json": row["meta_json"] or {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/{asset_id}/view", operation_id="commerce_assets_view")
async def view_asset(
    asset_id: UUID,
    ttl_minutes: int = Query(60, ge=1, le=24 * 60),
    user_id: UUID = Depends(require_user),
) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "select id, user_id, storage_ref from public.media_assets where id=$1",
            asset_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="asset_not_found")
    if UUID(str(row["user_id"])) != user_id:
        raise HTTPException(status_code=403, detail="forbidden")

    storage = AzureStorageService()
    c, b = _parse_az_storage_ref(str(row["storage_ref"]))
    url = storage.get_blob_sas_url(container=c, blob_name=b, expires_in_s=int(ttl_minutes) * 60, permission="r")
    return {"asset_id": str(asset_id), "url": url, "ttl_minutes": ttl_minutes}