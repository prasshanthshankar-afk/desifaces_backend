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
# - rows persisted as studio='fusion' are treated as Video and are eligible only
#   when they are not classified as internal scene/segment children.
_STRONG_INTERNAL_RE = re.compile(
    r"story_dialogue_workflow_id|dialogue_turn_id|story_audio|story_voice|"
    r"child_render|child_role|internal_child|child_job_of_billable_longform_parent|"
    r"suppress_pricing|pricing_suppressed|"
    r"parent_longform_job_id|billing_parent_job_id|parent_story_job_id",
    re.IGNORECASE,
)
_SEGMENT_RE = re.compile(
    r"segment_id|segment_index|segment_number|scene_id|scene_index|shot_id|shot_index",
    re.IGNORECASE,
)
_STORY_CONTEXT_RE = re.compile(r"story_id|workflow_id|participant_id", re.IGNORECASE)


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
        return dict(value)
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


def _authoritative_final(item: Dict[str, Any]) -> bool:
    """Return True for explicit final-output markers on the row itself.

    A generic share_url is intentionally not sufficient. Child/scene artifacts may be
    individually playable or shareable while still being internal implementation
    details. Final status must be expressed by final_video_url/final_storage_path or
    an explicit render/output role.
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
        for key in ("final_video_url", "final_storage_path"):
            if str(source.get(key) or "").strip():
                return True

    return False


def is_internal_customer_hidden_item(item: Dict[str, Any]) -> bool:
    """Classify execution-only Story/Fusion media that must not reach Saved Work."""
    studio = _studio(item)
    if studio == "face":
        return False

    text = _text(item)
    authoritative_final = _authoritative_final(item)

    # Explicit final video rows can retain scene/parent traceability metadata.
    if studio == "video" and authoritative_final:
        return False

    if _STRONG_INTERNAL_RE.search(text):
        return True

    # Scene/segment markers are definitive for video/fusion execution artifacts.
    if studio == "video" and _SEGMENT_RE.search(text):
        return True

    # Story-produced audio is execution material even when older rows lack the
    # newer child_render metadata. Require both story context and a scene/segment
    # marker so standalone Audio Studio clips are preserved.
    if studio == "audio" and _STORY_CONTEXT_RE.search(text) and _SEGMENT_RE.search(text):
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
        if keys and keys.intersection(seen):
            continue
        seen.update(keys)
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
    """Fetch raw media rows used to classify Story/Fusion internals and Fusion finals."""
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
            # Never turn a policy-side read-model discrepancy into a 500. The base
            # dashboard service remains available as a conservative fallback.
            return []


def _signer() -> AzureBlobSasSigner:
    return AzureBlobSasSigner.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)


def _normalize_visible_raw_rows(rows: Iterable[Dict[str, Any]], *, wanted_studio: str) -> List[Dict[str, Any]]:
    signer = _signer()
    out: List[Dict[str, Any]] = []
    for row in rows:
        raw_studio = str(row.get("studio") or "").strip().lower()
        if wanted_studio == "audio" and raw_studio != "audio":
            continue
        if wanted_studio == "fusion" and raw_studio != "fusion":
            continue
        if is_internal_customer_hidden_item(row):
            continue
        normalized = _normalize_library_item(row, signer)
        if normalized and not is_internal_customer_hidden_item(normalized):
            out.append(_ensure_video_thumbnail(normalized))
    return out


async def _public_face_items(pool: asyncpg.Pool, user_id: str) -> List[Dict[str, Any]]:
    payload = await _base_get_dashboard_library(pool, user_id, asset_type="face", limit=100, offset=0)
    return [dict(x) for x in (payload.get("items") or []) if isinstance(x, dict)]


async def _public_audio_items(pool: asyncpg.Pool, user_id: str, raw_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if raw_rows:
        return _normalize_visible_raw_rows(raw_rows, wanted_studio="audio")

    # Conservative fallback if the raw read model cannot be queried. Filter the
    # normalized base rows using the same title/metadata classifier.
    payload = await _base_get_dashboard_library(pool, user_id, asset_type="audio", limit=100, offset=0)
    return [
        dict(x)
        for x in (payload.get("items") or [])
        if isinstance(x, dict) and not is_internal_customer_hidden_item(dict(x))
    ]


async def _public_video_items(pool: asyncpg.Pool, user_id: str, raw_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Preserve the mature base service's final-longform synthesis and video SAS
    # normalization, then supplement rows persisted as studio='fusion'.
    payload = await _base_get_dashboard_library(pool, user_id, asset_type="video", limit=100, offset=0)
    public: List[Dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        if is_internal_customer_hidden_item(candidate):
            continue
        public.append(_ensure_video_thumbnail(candidate))

    if raw_rows:
        public.extend(_normalize_visible_raw_rows(raw_rows, wanted_studio="fusion"))

    public = _dedupe(public)
    public.sort(key=_created_sort_key, reverse=True)
    return public


def _page(items: List[Dict[str, Any]], limit: int, offset: int) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 50), 100))
    safe_offset = max(0, int(offset or 0))
    return items[safe_offset : safe_offset + safe_limit]


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

    raw_rows = [] if normalized_type == "face" else await _policy_rows(pool, user_id)

    if normalized_type == "face":
        public = await _public_face_items(pool, user_id)
    elif normalized_type == "audio":
        public = await _public_audio_items(pool, user_id, raw_rows)
    elif normalized_type == "video":
        public = await _public_video_items(pool, user_id, raw_rows)
    else:
        # Build All from already-classified public sets. Internal Story/Fusion rows
        # therefore never consume pagination slots or leak through a broad base query.
        faces = await _public_face_items(pool, user_id)
        audio = await _public_audio_items(pool, user_id, raw_rows)
        videos = await _public_video_items(pool, user_id, raw_rows)
        public = _dedupe([*faces, *audio, *videos])
        public.sort(key=_created_sort_key, reverse=True)

    paged = _page(public, limit, offset)
    return {
        "items": paged,
        "total": len(public),
        "limit": max(1, min(int(limit or 50), 100)),
        "offset": max(0, int(offset or 0)),
        "source": "dashboard_public_media_policy",
        "partial": False,
        "display_scope": "customer_final_outputs",
    }


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
    videos: List[Dict[str, Any]] = []
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
