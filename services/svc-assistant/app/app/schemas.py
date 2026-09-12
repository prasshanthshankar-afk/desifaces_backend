from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AssistantContextLocator(BaseModel):
    surface: Literal["mobile", "web"] = "mobile"
    screen: str = Field(min_length=1, max_length=80)
    story_id: UUID | None = None
    scene_id: UUID | None = None
    participant_id: UUID | None = None


class AssistantChatIn(BaseModel):
    session_id: UUID | None = None
    message: str = Field(min_length=1, max_length=8000)
    context: AssistantContextLocator


class AssistantAction(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=160)
    requires_confirmation: bool = True


class AssistantPolicyView(BaseModel):
    restricted: bool = False
    category: str | None = None
    redacted: bool = False


class AssistantChatOut(BaseModel):
    session_id: UUID
    message_id: UUID
    answer: str
    context: dict
    suggested_actions: list[AssistantAction] = Field(default_factory=list)
    policy: AssistantPolicyView
    sources: list[str] = Field(default_factory=list)
