from fastapi import APIRouter, Depends, Query

from app.deps import get_current_user_id
from app.schemas.notifications import (
    NotificationListResponse,
    NotificationPreferencesResponse,
    NotificationPreferencesUpdateRequest,
    RegisterDeviceRequest,
)
from app.services.notification_service import NotificationService, get_notification_service

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    category: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
    svc: NotificationService = Depends(get_notification_service),
):
    return await svc.list_notifications(user_id=user_id, category=category, limit=limit, offset=offset)


@router.get("/unread-count")
async def unread_count(
    user_id: str = Depends(get_current_user_id),
    svc: NotificationService = Depends(get_notification_service),
):
    return {"unread_count": await svc.get_unread_count(user_id)}


@router.post("/{item_id}/read")
async def mark_read(
    item_id: str,
    user_id: str = Depends(get_current_user_id),
    svc: NotificationService = Depends(get_notification_service),
):
    await svc.mark_read(user_id=user_id, item_id=item_id)
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read(
    user_id: str = Depends(get_current_user_id),
    svc: NotificationService = Depends(get_notification_service),
):
    await svc.mark_all_read(user_id=user_id)
    return {"ok": True}


@router.get("/preferences", response_model=NotificationPreferencesResponse)
async def get_preferences(
    user_id: str = Depends(get_current_user_id),
    svc: NotificationService = Depends(get_notification_service),
):
    return await svc.get_preferences(user_id=user_id)


@router.put("/preferences", response_model=NotificationPreferencesResponse)
async def update_preferences(
    req: NotificationPreferencesUpdateRequest,
    user_id: str = Depends(get_current_user_id),
    svc: NotificationService = Depends(get_notification_service),
):
    return await svc.update_preferences(user_id=user_id, req=req)


@router.post("/devices/register")
async def register_device(
    req: RegisterDeviceRequest,
    user_id: str = Depends(get_current_user_id),
    svc: NotificationService = Depends(get_notification_service),
):
    await svc.register_device(user_id=user_id, req=req)
    return {"ok": True}