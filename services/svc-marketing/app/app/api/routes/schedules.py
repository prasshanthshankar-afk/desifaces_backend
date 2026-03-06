from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import AuthedUser, require_user
from app.config import settings
from app.db import get_pool
from app.domain.models import ScheduleIn, ScheduleOut
from app.repos.schedules_repo import SchedulesRepo


router = APIRouter(prefix="/api/marketing/admin", tags=["marketing-admin"])


def _require_admin(user: AuthedUser) -> None:
    if not settings.ADMIN_MARKETING_USER_ID or str(user.user_id) != settings.ADMIN_MARKETING_USER_ID:
        raise HTTPException(status_code=403, detail="Admin only")


@router.post("/schedules", response_model=ScheduleOut)
async def create_schedule(inp: ScheduleIn, user: AuthedUser = Depends(require_user)) -> ScheduleOut:
    _require_admin(user)
    pool = await get_pool()
    repo = SchedulesRepo(pool)
    schedule_id = await repo.create(inp)
    row = await repo.get(schedule_id)
    return repo.to_out(row)


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(user: AuthedUser = Depends(require_user)) -> list[ScheduleOut]:
    _require_admin(user)
    pool = await get_pool()
    repo = SchedulesRepo(pool)
    rows = await repo.list_all()
    return [repo.to_out(r) for r in rows]


@router.post("/schedules/{schedule_id}/toggle", response_model=ScheduleOut)
async def toggle_schedule(schedule_id: UUID, enabled: bool, user: AuthedUser = Depends(require_user)) -> ScheduleOut:
    _require_admin(user)
    pool = await get_pool()
    repo = SchedulesRepo(pool)
    await repo.set_enabled(schedule_id, enabled)
    row = await repo.get(schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return repo.to_out(row)