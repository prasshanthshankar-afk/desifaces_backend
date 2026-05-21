from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

NotificationCategory = Literal["jobs", "billing", "account", "support", "announcements"]
NotificationPriority = Literal["critical", "important", "info"]


class NotificationAction(BaseModel):
    label: Optional[str] = None
    route: Optional[str] = None


class NotificationItemResponse(BaseModel):
    id: str
    title: str
    body: str
    category: NotificationCategory
    priority: NotificationPriority
    event_type: str
    created_at: datetime
    is_read: bool
    image_url: Optional[str] = None
    action: Optional[NotificationAction] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class NotificationListResponse(BaseModel):
    items: List[NotificationItemResponse] = Field(default_factory=list)
    unread_count: int = 0


class NotificationPreferenceItem(BaseModel):
    category: NotificationCategory
    in_app_enabled: bool = True
    push_enabled: bool = True
    email_enabled: bool = True


class NotificationPreferencesResponse(BaseModel):
    items: List[NotificationPreferenceItem] = Field(default_factory=list)


class NotificationPreferencesUpdateRequest(BaseModel):
    items: List[NotificationPreferenceItem]


class RegisterDeviceRequest(BaseModel):
    expo_push_token: str
    platform: Literal["ios", "android", "web"]
    device_name: Optional[str] = None
    app_version: Optional[str] = None


class InternalRecipientChannels(BaseModel):
    in_app: bool = True
    push: bool = True
    email: bool = True


class InternalRecipient(BaseModel):
    user_id: str
    channels: InternalRecipientChannels = Field(default_factory=InternalRecipientChannels)


class InternalNotificationEventCreate(BaseModel):
    event_type: str
    category: NotificationCategory
    priority: NotificationPriority = "info"
    source_service: str
    source_ref_type: Optional[str] = None
    source_ref_id: Optional[str] = None
    actor_user_id: Optional[str] = None
    title: str
    body: str
    action_route: Optional[str] = None
    action_label: Optional[str] = None
    image_url: Optional[str] = None
    payload_json: Dict[str, Any] = Field(default_factory=dict)
    metadata_json: Dict[str, Any] = Field(default_factory=dict)
    dedupe_key: Optional[str] = None
    recipients: List[InternalRecipient]


class InternalNotificationEventCreateResponse(BaseModel):
    event_id: str
    deduped: bool = False