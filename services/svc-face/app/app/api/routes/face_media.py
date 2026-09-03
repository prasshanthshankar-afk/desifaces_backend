from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user_id
from app.db import get_pool
from app.services.azure_storage_service import AzureStorageService


router = APIRouter()


def _as_meta_dict(value: Any) -> dict[str, Any]:
    """Normalize media_assets.meta_json across JSONB/text compatibility rows."""
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(value)

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}

        return dict(parsed) if isinstance(parsed, Mapping) else {}

    return {}


@router.get("/assets/{media_asset_id}/read-url")
async def get_face_media_read_url(
    media_asset_id: UUID,
    user_id: str = Depends(get_current_user_id),
):
    """Return a refreshed read-only URL for a Face-owned MediaAsset.

    The MediaAsset is the durable identity artifact; clients must not depend on
    provider/job status forever just to render an approved or rejected Face.
    Ownership is checked against the authenticated user before SAS refresh.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """select id,user_id,kind,storage_ref,meta_json
            from public.media_assets where id=$1 and user_id=$2::uuid""",
            media_asset_id,
            user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="face_media_asset_not_found")

    try:
        read_url = await AzureStorageService().get_readonly_sas_url(
            storage_ref=str(row["storage_ref"] or ""),
            meta_json=_as_meta_dict(row["meta_json"]),
            hours=24,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"face_media_read_url_failed:{exc}") from exc
    if not read_url:
        raise HTTPException(status_code=404, detail="face_media_storage_reference_missing")

    return {
        "media_asset_id": str(row["id"]),
        "kind": str(row["kind"] or ""),
        "read_url": read_url,
    }
