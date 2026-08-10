from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

SupportTopic = Literal["billing", "technical_issue", "feature_request", "account", "partnership", "general"]
SupportProductArea = Literal["face", "audio", "fusion", "dashboard", "billing", "other"]
SupportPriority = Literal["low", "normal", "high"]

class SupportContactRequest(BaseModel):
    name: str
    email: str
    topic: SupportTopic
    product_area: SupportProductArea
    priority: SupportPriority = "normal"
    subject: str
    message: str
    attachment_urls: List[str] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)

class SupportContactResponse(BaseModel):
    request_id: str
    ack_sent: bool

class SupportMessageResponse(BaseModel):
    id: str
    sender_role: Literal["user", "support", "system"]
    body: str
    attachments_json: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: datetime

class SupportRequestResponse(BaseModel):
    id: str
    topic: str
    product_area: str
    priority: str
    subject: str
    status: str
    latest_message_at: datetime
    created_at: datetime
    messages: List[SupportMessageResponse] = Field(default_factory=list)

class SupportReplyRequest(BaseModel):
    body: str
    attachment_urls: List[str] = Field(default_factory=list)