from fastapi import APIRouter, Depends, Query
import asyncpg

from app.api.deps import get_db_pool, get_current_user_id
from app.services.dashboard_service import get_dashboard_header, get_dashboard_home, get_dashboard_library, request_refresh

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/home")
async def dashboard_home(
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
    force: bool = Query(default=False, description="If true, compute cache inline now"),
):
    return await get_dashboard_home(pool, user_id, force_refresh=force)


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
    return await get_dashboard_library(pool, user_id, asset_type=type, limit=limit, offset=offset)


@router.post("/refresh")
async def dashboard_refresh(
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
):
    await request_refresh(pool, user_id, reason="api_refresh")
    return {"ok": True}
