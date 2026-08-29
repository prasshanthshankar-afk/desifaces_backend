from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Set

import asyncpg

from app.services.blob_sas_service import AzureBlobSasSigner
from app.services.dashboard_service import (
    _as_dict_deep_loose,
    _normalize_library_item,
    get_dashboard_home as _base_get_dashboard_home,
    get_dashboard_library as _base_get_dashboard_library,
)
from app.settings import settings


# Customer-facing media policy.
#
# Saved Work is a product library, not an execution-artifact browser:
# - standalone Face Studio outputs remain visible;
# - standalone Audio Studio outputs remain visible;
# - Story/Fusion dialogue turns, scene clips, segment renders and child jobs are hidden;
# - Video means the final customer-facing Fusion output only;
# - rows persisted as studio='fusion' are treated as Video and are eligible when final.
_STRONG_INTERNAL_RE = re.compile(
    r"story_dialogue_workflow_id|dialogue_turn_id|"
    r"child_render|child_role|internal_child|child_job_of_billable_longform_parent|"
    r"suppress_pricing|pricing_suppressed|"
    r"parent_longform_job_id|billing_parent_job_id|parent_story_job_id",
    re.IGNORECASE,
)
_SEGMENT_RE = re.compile(
    r"segment_id|segment_index|segment_number|scene_id|scene_index|shot_id|shot_index",
    re.IGNORECASE,
)


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        parsed = dict(value)
        return parsed
    except Exception:
        return {}


def _text(value: Any) -> str:
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except Exception:
        return str(value or "")


def _studio(item: Dict[str, Any]) -> str:
    value = str(item.get("studio") or item.get("asset_type") or item.get("type") or "").strip().lower()
    if value == "fusion":
        return "video"
    if value in {"audio", "video", "face"}:
        return value
    if "audio" in value or "voice" in value:
        return "audio"
    if "video" in value or "fusion" in value:
        return "video"
    return value


def _explicit_final(item: Dict[str, Any]) -> bool:
    """Return True only for explicit final markers on the row itself.

    Do not treat an arbitrary nested mention of a parent's final URL as proof that a
    child row is itself customer-facing.
    """
    meta = _json_dict(item.get("metadata_json") or item.get("metadata") or item.get("meta_json") or {})
    reuse = _json_dict(item.get("reuse_payload_json") or item.get("reuse_payload") or {})

    for source in (item, meta, reuse):
        render_kind = str(source.get("render_kind") or "").strip().lower()
        output_role = str(source.get("output_role") or "").strip().lower()
        if render_kind in {"final", "final_output", "stitched", "composed"}:
            return True
        if output_role in {"final", "customer_final", "final_output"}:
            return True
        for key in ("final_video_url", "final_storage_path", "share_url"):
            if str(source.get(key) or "").strip():
                return True

    return False


def is_internal_customer_hidden_item(item: Dict[str, Any]) -> bool:
    """Classify execution-only Story/Fusion media that must not reach Saved Work."""
    studio = _studio(item)
    if studio == "face":
        return False

    # An explicit final marker on this row wins. Final stitched rows can retain
    # scene/parent context for traceability without becoming internal children.
    if _explicit_final(item):
        return False

    text = _text(item)

    if _STRONG_INTERNAL_RE.search(text):
        return True

    # Scene/segment markers are definitive for video/fusion execution artifacts.
    # Audio is hidden on strong Story/Fusion markers above so a deliberately saved
    # standalone audio clip is not discarded merely because its context mentions a scene.
    if studio == "video" and _SEGMENT_RE.search(text):
        return True

    return False


def _identity_keys(item: Dict[str, Any]) -> Set[str]:
    reuse = _as_dict_deep_loose(item.get("reuse_payload") or item.get("reuse_payload_json"))
    values = (
        item.get("library_id"),
        item.get("source_job_id"),
        item.get("artifact_id"),
        item.get("media_asset_id"),
        item.get("preview_url"),
        item.get("download_url"),
        reuse.get("source_job_id"),
        reuse.get("video_artifact_id"),
        reuse.get("audio_artifact_id"),
        reuse.get("media_asset_id"),
        reuse.get("video_url"),
        reuse.get("audio_url"),
    )
    return {str(v).strip() for v in values if str(v or "").strip()}


def _created_sort_key(item: Dict[str, Any]) -> float:
    raw = item.get("created_at") or item.get("updated_at")
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass
    return 0.0


def _dedupe(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in items:
        keys = _identity_keys(item)
        stable = next(iter(sorted(keys)), "")
        if stable and stable in seen:
            continue
        if stable:
            seen.add(stable)
        out.append(item)
    return out


def _ensure_video_thumbnail(item: Dict[str, Any]) -> Dict[str, Any]:
    if _studio(item) != "video":
        return item
    if str(item.get("thumbnail_url") or "").strip():
        return item
    reuse = _as_dict_deep_loose(item.get("reuse_payload"))
    fallback = str(reuse.get("thumbnail_url") or reuse.get("poster_url") or item.get("poster_url") or "").strip()
    if fallback:
        item = dict(item)
        item["thumbnail_url"] = fallback
    return item


async def _policy_rows(pool: asyncpg.Pool, user_id: str) -> List[Dict[str, Any]]:
    """Fetch the raw media rows needed to classify hidden children and Fusion finals."""
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                select *
                from public.v_dashboard_asset_library
                where user_id = $1::uuid
                  and lower(coalesce(studio, '')) in ('audio', 'video', 'fusion')
                order by created_at desc nulls last
                limit 1000
                """,
                user_id,
            )
            return [dict(row) for row in rows]
        except Exception:
            # The base dashboard service remains authoritative if the policy read
            # model is temporarily unavailable; never convert this into a 500.
            return []


async def _public_library_items(
    pool: asyncpg.Pool,
    user_id: str,
    asset_type: str,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    base = await _base_get_dashboard_library(
        pool,
        user_id,
        asset_type=asset_type,
        limit=limit,
        offset=offset,
    )
    items = [dict(x) for x in (base.get("items") or []) if isinstance(x, dict)]

    if asset_type == "face":
        return {**base, "items": items, "total": len(items), "source": f"{base.get('source') or 'dashboard'}+public_media_policy"}

    raw_rows = await _policy_rows(pool, user_id)
    hidden_keys: Set[str] = set()
    public_fusion_rows: List[Dict[str, Any]] = []

    for row in raw_rows:
        if is_internal_customer_hidden_item(row):
            hidden_keys.update(_identity_keys(row))
            continue
        if str(row.get("studio") or "").strip().lower() == "fusion":
            public_fusion_rows.append(row)

    public: List[Dict[str, Any]] = []
    for item in items:
        if _identity_keys(item) & hidden_keys:
            continue
        if is_internal_customer_hidden_item(item):
            continue
        public.append(_ensure_video_thumbnail(item))

    # The legacy type=video SQL matches studio='video' literally. Preserve its
    # longform/final synthesis, then add customer-facing rows persisted as
    # studio='fusion'. This is the missing path that prevented valid Fusion finals
    # from appearing in Dashboard Recent Videos.
    if asset_type == "video" and public_fusion_rows:
        signer = AzureBlobSasSigner.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
        for row in public_fusion_rows:
            normalized = _normalize_library_item(row, signer)
            if normalized and not is_internal_customer_hidden_item(normalized):
                public.append(_ensure_video_thumbnail(normalized))

    public = _dedupe(public)
    public.sort(key=_created_sort_key, reverse=True)

    # The API already caps limit at 100. Base results are already paged; appended
    # Fusion rows are merged and then re-capped to the requested page size.
    public = public[: max(1, int(limit or 50))]

    return {
        **base,
        "items": public,
        "total": len(public),
        "source": f"{base.get('source') or 'dashboard'}+public_media_policy",
        "partial": False,
        "display_scope": "customer_final_outputs",
    }


async def get_dashboard_library(
    pool: asyncpg.Pool,
    user_id: str,
    asset_type: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    normalized_type = str(asset_type or "all").strip().lower()
    if normalized_type not in {"all", "face", "audio", "video"}:
        normalized_type = "all"
    return await _public_library_items(pool, user_id, normalized_type, limit, offset)


def _home_video_item(item: Dict[str, Any]) -> Dict[str, Any]:
    reuse = _as_dict_deep_loose(item.get("reuse_payload"))
    video_url = str(reuse.get("video_url") or item.get("preview_url") or item.get("download_url") or "").strip()
    thumbnail = str(item.get("thumbnail_url") or reuse.get("thumbnail_url") or reuse.get("poster_url") or "").strip() or None
    return {
        "id": item.get("artifact_id") or item.get("media_asset_id") or item.get("library_id"),
        "library_id": item.get("library_id"),
        "title": item.get("title") or "Video",
        "created_at": item.get("created_at"),
        "status": item.get("status") or "ready",
        "video_url": video_url,
        "url": video_url,
        "thumbnail_url": thumbnail,
        "poster_url": thumbnail,
        "preview_url": item.get("preview_url"),
        "download_url": item.get("download_url"),
        "artifact_id": item.get("artifact_id"),
        "media_asset_id": item.get("media_asset_id"),
        "source_job_id": item.get("source_job_id"),
        "meta": {
            "artifact_id": item.get("artifact_id"),
            "media_asset_id": item.get("media_asset_id"),
            "source_job_id": item.get("source_job_id"),
            "video_url": video_url,
            "thumbnail_url": thumbnail,
            "poster_url": thumbnail,
            "display_scope": "customer_final_outputs",
        },
    }


async def get_dashboard_home(
    pool: asyncpg.Pool,
    user_id: str,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    home = await _base_get_dashboard_home(pool, user_id, force_refresh=force_refresh)
    video_payload = await get_dashboard_library(pool, user_id, asset_type="video", limit=10, offset=0)
    videos = []
    for item in video_payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if is_internal_customer_hidden_item(item):
            continue
        home_item = _home_video_item(item)
        if home_item.get("video_url"):
            videos.append(home_item)
    home["video_carousel"] = videos
    home["video_display_scope"] = "customer_final_outputs"
    return home
