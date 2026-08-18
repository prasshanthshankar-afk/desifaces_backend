"""Canonical desifaces-v3 domain contracts.

The goal of these models is to establish cross-capability vocabulary before Face,
Audio, Fusion, Story, Conversation, or Director-specific rewrites begin.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from .common import V3ContractModel


class EntityState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ParticipantKind(StrEnum):
    PERSON = "person"
    CHARACTER = "character"
    PET = "pet"
    OTHER = "other"


class MediaKind(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    OTHER = "other"


class MediaRole(StrEnum):
    SOURCE = "source"
    INTERMEDIATE = "intermediate"
    PREVIEW = "preview"
    FINAL = "final"
    THUMBNAIL = "thumbnail"


class GenerationKind(StrEnum):
    FACE = "face"
    AUDIO = "audio"
    FUSION = "fusion"
    STORY = "story"
    CONVERSATION = "conversation"
    DIRECTOR = "director"
    OTHER = "other"


class JobState(StrEnum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    EXPIRED = "expired"


class SafetyState(StrEnum):
    PENDING = "pending"
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"


class ProviderExecutionState(StrEnum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class AccountRef(V3ContractModel):
    account_id: UUID


class UserRef(V3ContractModel):
    user_id: UUID
    account_id: UUID


class ParticipantRef(V3ContractModel):
    participant_id: UUID
    kind: ParticipantKind
    display_name: str | None = Field(default=None, max_length=200)


class ProjectRef(V3ContractModel):
    project_id: UUID
    account_id: UUID


class ConversationRef(V3ContractModel):
    conversation_id: UUID
    project_id: UUID | None = None


class StoryRef(V3ContractModel):
    story_id: UUID
    project_id: UUID | None = None


class SceneRef(V3ContractModel):
    scene_id: UUID
    story_id: UUID
    sequence: int = Field(ge=0)


class MediaAsset(V3ContractModel):
    """Canonical durable media identity.

    ``storage_uri`` is a stable storage reference (for example ``az://...``), not
    a temporary signed/SAS delivery URL. ``source_media_ids`` describes lineage;
    the persistence layer stores that lineage relationally.
    """

    media_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    owner_user_id: UUID | None = None
    project_id: UUID | None = None
    kind: MediaKind
    role: MediaRole
    lifecycle_state: EntityState = EntityState.ACTIVE
    mime_type: str | None = Field(default=None, max_length=150)
    storage_uri: str = Field(min_length=1, max_length=4000)
    sha256: str | None = Field(default=None, max_length=128)
    size_bytes: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    thumbnail_media_id: UUID | None = None
    source_media_ids: tuple[UUID, ...] = ()
    parent_job_id: UUID | None = None
    retention_until: datetime | None = None
    deleted_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class GenerationRequest(V3ContractModel):
    generation_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    requested_by_user_id: UUID
    project_id: UUID | None = None
    kind: GenerationKind
    participant_ids: tuple[UUID, ...] = ()
    source_media_ids: tuple[UUID, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)
    pricing_quote_id: UUID | None = None
    safety_state: SafetyState = SafetyState.PENDING
    created_at: datetime


class GenerationJob(V3ContractModel):
    job_id: UUID = Field(default_factory=uuid4)
    generation_id: UUID
    parent_job_id: UUID | None = None
    job_type: str = Field(default="root", min_length=1, max_length=100)
    state: JobState = JobState.SUBMITTED
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    provider_execution_ids: tuple[UUID, ...] = ()
    output_media_ids: tuple[UUID, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ProviderExecution(V3ContractModel):
    execution_id: UUID = Field(default_factory=uuid4)
    job_id: UUID
    provider: str = Field(min_length=1, max_length=100)
    capability: str = Field(min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    state: ProviderExecutionState = ProviderExecutionState.PLANNED
    provider_request_id: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)
    attempt: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SafetyDecision(V3ContractModel):
    safety_decision_id: UUID = Field(default_factory=uuid4)
    generation_id: UUID
    state: SafetyState
    policy_code: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)
    provider: str | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
