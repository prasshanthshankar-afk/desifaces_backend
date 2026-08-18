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
        if self.scope_type == StudioScopeType.PARTICIPANT and self.participant_id is None:
            raise ValueError("participant_scope_requires_participant_id")
        if self.scope_type == StudioScopeType.DIALOGUE_TURN and self.dialogue_turn_id is None:
            raise ValueError("dialogue_turn_scope_requires_dialogue_turn_id")
        if self.scope_type == StudioScopeType.SCENE and self.scene_id is None:
            raise ValueError("scene_scope_requires_scene_id")
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
