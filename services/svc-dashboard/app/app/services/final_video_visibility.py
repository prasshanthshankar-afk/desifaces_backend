from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return dict(value or {})
    except Exception:
        return {}


def _storage_ref_parts(value: Any, default_container: str = "") -> Tuple[str, str]:
    """Resolve a media storage reference into Azure container/blob parts.

    V3 media rows can carry a full blob URL, azure://container/blob, an explicit
    container/blob value, or a blob path relative to the configured output
    container. The dashboard must normalize all four forms before SAS signing.
    """
    raw = _clean_text(value)
    if not raw:
        return "", ""

    default = _clean_text(default_container).strip("/")

    try:
        parsed = urlparse(raw)
    except Exception:
        parsed = None

    if parsed and parsed.scheme.lower() == "azure":
        container = _clean_text(parsed.netloc).strip("/")
        blob = _clean_text(parsed.path).lstrip("/")
        return (container, blob) if container and blob else ("", "")

    if parsed and parsed.scheme.lower() in {"http", "https"}:
        path = _clean_text(parsed.path).lstrip("/")
        container, sep, blob = path.partition("/")
        return (container, blob) if sep and container and blob else ("", "")

    path = raw.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if not path:
        return "", ""

    if default:
        prefix = f"{default}/"
        blob = path[len(prefix):] if path.startswith(prefix) else path
        return (default, blob) if blob else ("", "")

    container, sep, blob = path.partition("/")
    return (container, blob) if sep and container and blob else ("", "")


def _is_browser_media_url(value: Any) -> bool:
    text = _clean_text(value).lower()
    return text.startswith("https://") or text.startswith("http://")


def _video_key(item: Mapping[str, Any]) -> str:
    reuse = _as_dict(item.get("reuse_payload"))
    meta = _as_dict(item.get("meta"))
    return _clean_text(
        item.get("media_asset_id")
        or reuse.get("media_asset_id")
        or meta.get("media_asset_id")
        or item.get("artifact_id")
        or item.get("library_id")
        or item.get("video_url")
        or reuse.get("video_url")
        or item.get("preview_url")
        or item.get("download_url")
    )


def _created_at_epoch(item: Mapping[str, Any]) -> float:
    value = item.get("created_at")
    if isinstance(value, datetime):
        dt = value
    else:
        text = _clean_text(value)
        if not text:
            return 0.0
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp()
    except Exception:
        return 0.0


def merge_final_video_items(
    existing: Iterable[Mapping[str, Any]],
    canonical_finals: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge canonical V3 finals with existing final-only dashboard items.

    Canonical final_media_id rows win on duplicate keys. This guarantees exactly
    one customer-visible item even if the dashboard view later indexes the same
    media asset independently.
    """
    merged_by_key: Dict[str, Dict[str, Any]] = {}
    unkeyed: List[Dict[str, Any]] = []

    for source in (canonical_finals, existing):
        for raw in source:
            item = dict(raw)
            key = _video_key(item)
            if not key:
                unkeyed.append(item)
                continue
            if key not in merged_by_key:
                merged_by_key[key] = item

    merged = list(merged_by_key.values()) + unkeyed
    merged.sort(key=_created_at_epoch, reverse=True)
    return merged


async def _fetch_canonical_v3_final_items(pool: Any, user_id: str) -> List[Dict[str, Any]]:
    """Read completed V3 Story/Fusion canonical finals from final_media_id.

    The workflow relationship is authoritative. media_assets.role is deliberately
    not used because valid canonical finals may still carry role='preview'.
    """
    from app.settings import settings
    from app.services.blob_sas_service import AzureBlobSasSigner
    from app.services.dashboard_service import _normalize_library_item

    sql = r'''
    select
      ('v3-final:' || w.final_media_id::text) as library_id,
      w.owner_user_id as user_id,
      'video'::text as studio,
      'video'::text as asset_type,
      coalesce(
        nullif(w.metadata_json->>'title', ''),
        nullif(w.metadata_json->>'story_title', ''),
        'Story Final Video'
      ) as title,
      'ready'::text as status,
      coalesce(w.updated_at, m.created_at) as created_at,
      coalesce(tm.storage_ref, poster.storage_ref) as thumbnail_url,
      m.storage_ref as preview_url,
      m.storage_ref as download_url,
      null::uuid as artifact_id,
      m.id as media_asset_id,
      null::uuid as source_job_id,
      jsonb_strip_nulls(jsonb_build_object(
        'media_asset_id', m.id,
        'video_media_asset_id', m.id,
        'video_url', m.storage_ref,
        'thumbnail_url', coalesce(tm.storage_ref, poster.storage_ref),
        'poster_url', coalesce(tm.storage_ref, poster.storage_ref),
        'v3_workflow_id', w.workflow_id,
        'story_id', w.story_id,
        'project_id', w.project_id,
        'render_kind', 'final',
        'output_role', 'final',
        'canonical_final', true,
        'display_scope', 'final_outputs'
      )) as reuse_payload_json,
      (
        coalesce(m.meta_json, '{}'::jsonb)
        || jsonb_build_object(
          'v3_workflow_id', w.workflow_id,
          'story_id', w.story_id,
          'project_id', w.project_id,
          'render_kind', 'final',
          'output_role', 'final',
          'canonical_final', true,
          'display_scope', 'final_outputs'
        )
      ) as metadata_json
    from public.v3_studio_workflows w
    join public.media_assets m
      on m.id = w.final_media_id
    left join public.media_assets tm
      on tm.id = m.thumbnail_media_id
     and tm.deleted_at is null
     and lower(coalesce(tm.lifecycle_state, 'active')) = 'active'
     and (
       lower(coalesce(tm.kind, '')) = 'image'
       or lower(coalesce(tm.content_type, '')) like 'image/%'
     )
    left join lateral (
      select im.storage_ref
      from public.media_assets im
      where im.user_id = w.owner_user_id
        and im.project_id = w.project_id
        and im.deleted_at is null
        and lower(coalesce(im.lifecycle_state, 'active')) = 'active'
        and im.storage_ref is not null
        and im.storage_ref <> ''
        and (
          lower(coalesce(im.kind, '')) = 'image'
          or lower(coalesce(im.content_type, '')) like 'image/%'
        )
      order by
        case when lower(coalesce(im.role, '')) in ('thumbnail', 'poster') then 0 else 1 end,
        im.created_at desc nulls last,
        im.id desc
      limit 1
    ) poster on true
    where w.owner_user_id = $1::uuid
      and lower(coalesce(w.state, '')) = 'completed'
      and lower(coalesce(w.current_stage, '')) = 'fusion'
      and w.final_media_id is not null
      and m.user_id = w.owner_user_id
      and m.deleted_at is null
      and lower(coalesce(m.lifecycle_state, 'active')) = 'active'
      and m.storage_ref is not null
      and m.storage_ref <> ''
      and (
        lower(coalesce(m.kind, '')) = 'video'
        or lower(coalesce(m.content_type, '')) like 'video/%'
      )
    order by coalesce(w.updated_at, m.created_at) desc nulls last
    '''

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, user_id)
    except Exception:
        # Dashboard availability must not depend on V3 tables being present in an
        # older environment. Existing final-only behavior remains intact.
        return []

    try:
        signer = AzureBlobSasSigner.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
    except Exception:
        return []

    video_container = (
        _clean_text(os.getenv("AZURE_FINAL_VIDEO_CONTAINER"))
        or _clean_text(os.getenv("AZURE_VIDEO_OUTPUT_CONTAINER"))
        or _clean_text(getattr(settings, "AZURE_STORAGE_CONTAINER_NAME", ""))
        or "video-output"
    )
    image_container = (
        _clean_text(os.getenv("AZURE_FACE_OUTPUT_CONTAINER"))
        or _clean_text(getattr(settings, "AZURE_STORAGE_CONTAINER_NAME", ""))
        or "face-output"
    )

    items: List[Dict[str, Any]] = []
    for row in rows:
        candidate = dict(row)

        preview_container, preview_blob = _storage_ref_parts(candidate.get("preview_url"), video_container)
        if preview_container and preview_blob:
            candidate["preview_container"] = preview_container
            candidate["preview_storage_path"] = preview_blob
            candidate["download_container"] = preview_container
            candidate["download_storage_path"] = preview_blob

        thumbnail_container, thumbnail_blob = _storage_ref_parts(candidate.get("thumbnail_url"), image_container)
        if thumbnail_container and thumbnail_blob:
            candidate["thumbnail_container"] = thumbnail_container
            candidate["thumbnail_storage_path"] = thumbnail_blob

        normalized = _normalize_library_item(candidate, signer)
        if not normalized:
            continue

        # The canonical final contract returned to browsers contains one signed
        # HTTP(S) URL everywhere. Never let the raw media_assets.storage_ref win
        # through reuse_payload.video_url or Dashboard home card construction.
        signed_video_url = _clean_text(normalized.get("preview_url") or normalized.get("download_url"))
        if not _is_browser_media_url(signed_video_url):
            continue

        normalized["preview_url"] = signed_video_url
        normalized["download_url"] = signed_video_url
        normalized["canonical_final"] = True
        normalized["render_kind"] = "final"
        normalized["output_role"] = "final"
        normalized["display_scope"] = "final_outputs"

        signed_thumbnail_url = _clean_text(normalized.get("thumbnail_url"))
        if not _is_browser_media_url(signed_thumbnail_url):
            signed_thumbnail_url = ""
            normalized["thumbnail_url"] = None

        reuse = _as_dict(normalized.get("reuse_payload"))
        reuse.update(
            {
                "video_url": signed_video_url,
                "canonical_final": True,
                "render_kind": "final",
                "output_role": "final",
                "display_scope": "final_outputs",
            }
        )
        if signed_thumbnail_url:
            reuse["thumbnail_url"] = signed_thumbnail_url
            reuse["poster_url"] = signed_thumbnail_url
        else:
            reuse.pop("thumbnail_url", None)
            reuse.pop("poster_url", None)
        normalized["reuse_payload"] = reuse
        items.append(normalized)
    return items


async def enrich_dashboard_library_with_v3_finals(
    pool: Any,
    user_id: str,
    response: Mapping[str, Any],
    *,
    asset_type: str,
    requested_limit: int,
    requested_offset: int,
) -> Dict[str, Any]:
    kind = _clean_text(asset_type).lower() or "all"
    resp = dict(response)
    if kind not in {"all", "video"}:
        return resp

    canonical = await _fetch_canonical_v3_final_items(pool, user_id)
    if not canonical:
        return resp

    existing = [dict(x) for x in (resp.get("items") or []) if isinstance(x, Mapping)]
    existing_keys = {_video_key(x) for x in existing if _video_key(x)}
    missing_count = sum(1 for x in canonical if _video_key(x) and _video_key(x) not in existing_keys)

    merged = merge_final_video_items(existing, canonical)
    start = max(0, int(requested_offset or 0))
    size = max(1, min(int(requested_limit or 50), 100))
    resp["items"] = merged[start:start + size]

    prior_total = resp.get("total")
    try:
        total = int(prior_total)
    except Exception:
        total = len(existing)
    resp["total"] = max(total + missing_count, len(merged))
    resp["limit"] = size
    resp["offset"] = start
    resp["source"] = "v_dashboard_asset_library+v3_studio_workflows.final_media_id"
    resp["partial"] = False if kind == "video" else bool(resp.get("partial", False))
    resp["canonical_final_policy"] = "workflow.final_media_id"
    return resp


def _home_card(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    reuse = _as_dict(item.get("reuse_payload"))
    video_url = _clean_text(
        item.get("preview_url")
        or item.get("download_url")
        or reuse.get("video_url")
    )
    if not _is_browser_media_url(video_url):
        return None
    media_id = _clean_text(item.get("media_asset_id") or reuse.get("media_asset_id"))
    library_id = _clean_text(item.get("library_id"))
    thumbnail = _clean_text(
        item.get("thumbnail_url")
        or reuse.get("thumbnail_url")
        or reuse.get("poster_url")
    )
    return {
        "id": media_id or library_id,
        "library_id": library_id or None,
        "title": item.get("title") or "Story Final Video",
        "created_at": item.get("created_at"),
        "status": item.get("status") or "ready",
        "video_url": video_url,
        "url": video_url,
        "thumbnail_url": thumbnail or None,
        "poster_url": thumbnail or None,
        "preview_url": video_url,
        "download_url": video_url,
        "artifact_id": item.get("artifact_id"),
        "media_asset_id": media_id or None,
        "source_job_id": item.get("source_job_id"),
        "canonical_final": True,
        "render_kind": "final",
        "output_role": "final",
        "display_scope": "final_outputs",
        "meta": {
            "media_asset_id": media_id or None,
            "video_url": video_url,
            "thumbnail_url": thumbnail or None,
            "poster_url": thumbnail or None,
            "canonical_final": True,
            "render_kind": "final",
            "output_role": "final",
            "display_scope": "final_outputs",
        },
    }


async def enrich_dashboard_home_with_v3_finals(
    pool: Any,
    user_id: str,
    response: Mapping[str, Any],
    *,
    limit: int = 10,
) -> Dict[str, Any]:
    resp = dict(response)
    canonical = await _fetch_canonical_v3_final_items(pool, user_id)
    if not canonical:
        return resp

    canonical_cards = [card for card in (_home_card(x) for x in canonical) if card]
    existing_cards = [dict(x) for x in (resp.get("video_carousel") or []) if isinstance(x, Mapping)]
    merged = merge_final_video_items(existing_cards, canonical_cards)
    resp["video_carousel"] = merged[: max(1, min(int(limit or 10), 25))]
    resp["video_visibility_policy"] = "canonical_final_only"
    return resp
