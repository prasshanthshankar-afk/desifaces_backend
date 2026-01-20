import asyncpg
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.settings import settings

import json
from typing import Any, Dict

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

def _record_to_dict(r) -> Dict[str, Any]:
    return {
        "user_id": str(r["user_id"]),
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "gauges": _coerce_json(r["gauges_json"]) or {},
        "alerts": _coerce_json(r["alerts_json"]) or [],
        "face_carousel": _coerce_json(r["face_carousel_json"]) or [],
        "video_carousel": _coerce_json(r["video_carousel_json"]) or [],
        "header": _coerce_json(r["header_json"]) or {},
    }

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
        "gauges": r["gauges_json"],
        "alerts": r["alerts_json"],
        "face_carousel": r["face_carousel_json"],
        "video_carousel": r["video_carousel_json"],
        "header": r["header_json"],
    }


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
            return _record_to_dict(row)

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

        return _record_to_dict(row)


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