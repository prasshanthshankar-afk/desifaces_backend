from fastapi import APIRouter, Depends

from app.deps import require_internal_service_auth
from app.schemas.notifications import (
    InternalNotificationEventCreate,
    InternalNotificationEventCreateResponse,
)
from app.services.notification_service import NotificationService, get_notification_service

router = APIRouter(
    prefix="/api/internal/notifications",
    tags=["internal-notifications"],
)


@router.post("/events", response_model=InternalNotificationEventCreateResponse)
async def create_internal_notification_event(
    req: InternalNotificationEventCreate,
    svc: NotificationService = Depends(get_notification_service),
    _svc: bool = Depends(require_internal_service_auth),
):
    result = await svc.emit_internal_event(req=req)
    return InternalNotificationEventCreateResponse(
        event_id=str(result["event_id"]),
        deduped=bool(result.get("deduped", False)),
    )