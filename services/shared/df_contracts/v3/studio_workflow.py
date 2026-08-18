"""Canonical Face -> Audio -> Fusion workflow contracts with HITL stage gates."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from .common import V3ContractModel


class StudioWorkflowState(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class StudioStageType(StrEnum):
    FACE = "face"
    AUDIO = "audio"
    FUSION = "fusion"
    STORY_FINAL = "story_final"


class StudioStageState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    GENERATING = "generating"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    SKIPPED = "skipped"


class StudioScopeType(StrEnum):
    PARTICIPANT = "participant"
    DIALOGUE_TURN = "dialogue_turn"
    SCENE = "scene"
    STORY = "story"


class ReviewDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISE = "revise"


class StudioArtifactRef(V3ContractModel):
    media_id: UUID
    role: str = Field(min_length=1, max_length=100)
    source_stage_run_id: UUID | None = None
    is_active: bool = True


class StudioReviewItem(V3ContractModel):
    review_item_id: UUID
    stage_run_id: UUID
    media_id: UUID
    decision: ReviewDecision = ReviewDecision.PENDING
    reviewer_user_id: UUID | None = None
    feedback: str | None = Field(default=None, max_length=12000)
    decided_at: datetime | None = None


class StudioStageView(V3ContractModel):
    stage_run_id: UUID
    workflow_id: UUID
    stage_type: StudioStageType
    scope_type: StudioScopeType
    participant_id: UUID | None = None
    scene_id: UUID | None = None
    dialogue_turn_id: UUID | None = None
    state: StudioStageState
    generation_request_id: UUID | None = None
    generation_job_id: UUID | None = None
    inputs: tuple[StudioArtifactRef, ...] = ()
    outputs: tuple[StudioArtifactRef, ...] = ()
    reviews: tuple[StudioReviewItem, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_scope(self):
        ids = (self.participant_id, self.scene_id, self.dialogue_turn_id)
        if self.scope_type == StudioScopeType.PARTICIPANT:
            if self.participant_id is None or self.scene_id is not None or self.dialogue_turn_id is not None:
                raise ValueError("invalid_participant_scope")
        elif self.scope_type == StudioScopeType.DIALOGUE_TURN:
            if self.dialogue_turn_id is None or self.participant_id is not None or self.scene_id is not None:
                raise ValueError("invalid_dialogue_turn_scope")
        elif self.scope_type == StudioScopeType.SCENE:
            if self.scene_id is None or self.participant_id is not None or self.dialogue_turn_id is not None:
                raise ValueError("invalid_scene_scope")
        elif self.scope_type == StudioScopeType.STORY and any(value is not None for value in ids):
            raise ValueError("invalid_story_scope")
        return self


class StudioWorkflowView(V3ContractModel):
    workflow_id: UUID
    account_id: UUID
    owner_user_id: UUID
    project_id: UUID
    story_id: UUID | None = None
    state: StudioWorkflowState
    current_stage: StudioStageType | None = None
    stages: tuple[StudioStageView, ...] = ()
    final_media_id: UUID | None = None
    next_action: str | None = None
    created_at: datetime
    updated_at: datetime
