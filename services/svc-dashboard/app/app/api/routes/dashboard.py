from fastapi import APIRouter, Depends, Query
import asyncpg

from app.api.deps import get_db_pool, get_current_user_id
from app.services.dashboard_service import get_dashboard_home, get_dashboard_header, request_refresh

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


@router.post("/refresh")
async def dashboard_refresh(
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
):
    await request_refresh(pool, user_id, reason="api_refresh")
    return {"ok": True}