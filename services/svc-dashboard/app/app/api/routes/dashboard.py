from fastapi import APIRouter, Depends, Query
import asyncpg

from app.api.deps import get_db_pool, get_current_user_id
from app.services.dashboard_service import get_dashboard_header, get_dashboard_home, get_dashboard_library, request_refresh
from app.services.final_video_visibility import (
    enrich_dashboard_home_with_v3_finals,
    enrich_dashboard_library_with_v3_finals,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/home")
async def dashboard_home(
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
    force: bool = Query(default=False, description="If true, compute cache inline now"),
):
    response = await get_dashboard_home(pool, user_id, force_refresh=force)
    return await enrich_dashboard_home_with_v3_finals(pool, user_id, response)


@router.get("/header")
async def dashboard_header(
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
):
    return await get_dashboard_header(pool, user_id)


@router.get("/library")
async def dashboard_library(
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
    type: str = Query(default="all", description="all | face | audio | video"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    asset_type = (type or "all").strip().lower()
    if asset_type not in {"all", "face", "audio", "video"}:
        asset_type = "all"

    # V3 canonical finals are merged before paging so Dashboard and Saved Work
    # observe one consistent final-video set. The API contract already caps a
    # page at 100 items; fetch the requested prefix from the existing read path.
    if asset_type in {"all", "video"}:
        base_limit = min(100, max(limit + offset, limit))
        response = await get_dashboard_library(
            pool,
            user_id,
            asset_type=asset_type,
            limit=base_limit,
            offset=0,
        )
        return await enrich_dashboard_library_with_v3_finals(
            pool,
            user_id,
            response,
            asset_type=asset_type,
            requested_limit=limit,
            requested_offset=offset,
        )

    return await get_dashboard_library(
        pool,
        user_id,
        asset_type=asset_type,
        limit=limit,
        offset=offset,
    )


@router.post("/refresh")
async def dashboard_refresh(
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
):
    await request_refresh(pool, user_id, reason="api_refresh")
    return {"ok": True}
