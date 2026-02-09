import asyncpg
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
import json

from app.settings import settings
from app.services.blob_sas_service import AzureBlobSasSigner, split_container_blob_from_url


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


async def _fetch_home_row(conn: asyncpg.Connection, user_id: str) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        """
        select user_id, updated_at, gauges_json, alerts_json, face_carousel_json, video_carousel_json, header_json
        from public.v_dashboard_home
        where user_id = $1::uuid
        """,
        user_id,
    )


def _record_to_dict(r: asyncpg.Record) -> Dict[str, Any]:
    return {
        "user_id": str(r["user_id"]),
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "gauges": _coerce_json(r["gauges_json"]) or {},
        "alerts": _coerce_json(r["alerts_json"]) or [],
        "face_carousel": _coerce_json(r["face_carousel_json"]) or [],
        "video_carousel": _coerce_json(r["video_carousel_json"]) or [],
        "header": _coerce_json(r["header_json"]) or {},
    }


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            s = v.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _is_recent(item: Dict[str, Any], days: int) -> bool:
    dt = _parse_dt(item.get("created_at")) or _parse_dt(item.get("updated_at"))
    if not dt:
        # safer for UX: treat unknown timestamps as recent so links don't expire unexpectedly
        return True
    return dt >= (datetime.now(timezone.utc) - timedelta(days=days))


def _get_storage_path(item: Dict[str, Any]) -> Optional[str]:
    meta = item.get("meta") or {}
    return meta.get("storage_path") or item.get("storage_path") or item.get("output_storage_path")


def _enrich_carousels_with_sas(resp: Dict[str, Any]) -> Dict[str, Any]:
    # TTL policy
    face_ttl_seconds = int(getattr(settings, "DASHBOARD_FACE_SAS_TTL_SECONDS", 2 * 24 * 3600))
    recent_video_ttl_seconds = int(getattr(settings, "DASHBOARD_RECENT_VIDEO_SAS_TTL_SECONDS", 15 * 24 * 3600))
    default_video_ttl_seconds = int(getattr(settings, "DASHBOARD_VIDEO_SAS_TTL_SECONDS", 24 * 3600))
    recent_window_days = int(getattr(settings, "DASHBOARD_RECENT_WINDOW_DAYS", 15))

    # Containers
    face_container = getattr(settings, "AZURE_FACE_OUTPUT_CONTAINER", "face-output")
    video_container = getattr(settings, "AZURE_VIDEO_OUTPUT_CONTAINER", "video-output")

    # Signer from connection string
    signer = AzureBlobSasSigner.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)

    # Face carousel: add image_url from meta.storage_path (usually blob-only path)
    for it in (resp.get("face_carousel") or []):
        if not isinstance(it, dict):
            continue

        sp = _get_storage_path(it)
        if sp:
            it["image_url"] = signer.sign_read_url(face_container, sp, face_ttl_seconds)
            continue

        # fallback: if item already has an image_url, re-sign it
        existing = it.get("image_url")
        parts = split_container_blob_from_url(existing) if existing else None
        it["image_url"] = signer.sign_read_url(parts[0], parts[1], face_ttl_seconds) if parts else None

    # Video carousel: ensure video_url SAS with long TTL (15 days) for recent items
    for it in (resp.get("video_carousel") or []):
        if not isinstance(it, dict):
            continue

        ttl = recent_video_ttl_seconds if _is_recent(it, recent_window_days) else default_video_ttl_seconds

        sp = _get_storage_path(it)
        if sp:
            it["video_url"] = signer.sign_read_url(video_container, sp, ttl)
            continue

        # fallback: parse existing video_url and re-sign with correct TTL
        existing = it.get("video_url")
        parts = split_container_blob_from_url(existing) if existing else None
        it["video_url"] = signer.sign_read_url(parts[0], parts[1], ttl) if parts else None

    return resp


async def get_dashboard_home(pool: asyncpg.Pool, user_id: str, force_refresh: bool = False) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        row = await _fetch_home_row(conn, user_id)

        # If missing row, optionally compute once inline (first-load experience)
        if row is None and settings.DASHBOARD_FORCE_REFRESH_ON_MISS:
            await conn.execute("select public.fn_dashboard_refresh_home_cache($1::uuid)", user_id)
            row = await _fetch_home_row(conn, user_id)

        if row is None:
            # Return stable empty contract
            return {
                "user_id": user_id,
                "updated_at": None,
                "gauges": {},
                "alerts": [],
                "face_carousel": [],
                "video_carousel": [],
                "header": {},
            }

        # If caller asked for force refresh, do it now
        if force_refresh:
            await conn.execute("select public.fn_dashboard_refresh_home_cache($1::uuid)", user_id)
            row = await _fetch_home_row(conn, user_id)
            resp = _record_to_dict(row)
            return _enrich_carousels_with_sas(resp)

        # If stale, enqueue refresh (non-blocking)
        updated_at: datetime = row["updated_at"]
        if updated_at:
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if age >= settings.DASHBOARD_STALE_SECONDS:
                await conn.execute(
                    "select public.fn_dashboard_enqueue_refresh($1::uuid, $2::text)",
                    user_id,
                    "stale_home",
                )

        resp = _record_to_dict(row)
        return _enrich_carousels_with_sas(resp)


async def get_dashboard_header(pool: asyncpg.Pool, user_id: str) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select user_id, updated_at, header_json
            from public.v_dashboard_home
            where user_id = $1::uuid
            """,
            user_id,
        )
        if row is None and settings.DASHBOARD_FORCE_REFRESH_ON_MISS:
            await conn.execute("select public.fn_dashboard_refresh_home_cache($1::uuid)", user_id)
            row = await conn.fetchrow(
                """
                select user_id, updated_at, header_json
                from public.v_dashboard_home
                where user_id = $1::uuid
                """,
                user_id,
            )

        if row is None:
            return {"user_id": user_id, "updated_at": None, "header": {}}

        return {
            "user_id": str(row["user_id"]),
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "header": _coerce_json(row["header_json"]) or {},
        }


async def request_refresh(pool: asyncpg.Pool, user_id: str, reason: str = "manual") -> None:
    async with pool.acquire() as conn:
        await conn.execute("select public.fn_dashboard_enqueue_refresh($1::uuid, $2::text)", user_id, reason)