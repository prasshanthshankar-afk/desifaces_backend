"""Canonical desifaces-v3 participant and storytelling contracts.

The domain is intentionally cardinality-neutral: one-person creation is the
one-participant case; two-person dialogue and larger groups use the same model.
Provider/UI composition labels such as ``single_person`` or ``two_people`` are
compatibility projections and are not canonical domain state.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .common import V3ContractModel
from .domain import EntityState, ParticipantKind


class StoryState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    GENERATING = "generating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ARCHIVED = "archived"


class SceneState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    GENERATING = "generating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DialogueTurnKind(StrEnum):
    SPEECH = "speech"
    NARRATION = "narration"
    ACTION = "action"


class Project(V3ContractModel):
    project_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    owner_user_id: UUID
    title: str = Field(default="Untitled Project", min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    lifecycle_state: EntityState = EntityState.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class Participant(V3ContractModel):
    """Durable character/person identity reused across Face, Audio and Fusion."""

    participant_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    project_id: UUID
    kind: ParticipantKind = ParticipantKind.PERSON
    display_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    default_locale: str | None = Field(default=None, max_length=100)
    primary_face_media_id: UUID | None = None
    reference_media_ids: tuple[UUID, ...] = ()
    voice_profile_ref: str | None = Field(default=None, max_length=500)
    voice_locale: str | None = Field(default=None, max_length=100)
    persona: dict[str, Any] = Field(default_factory=dict)
    continuity: dict[str, Any] = Field(default_factory=dict)
    lifecycle_state: EntityState = EntityState.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class Story(V3ContractModel):
    story_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    project_id: UUID
    title: str = Field(default="Untitled Story", min_length=1, max_length=300)
    synopsis: str | None = Field(default=None, max_length=10000)
    default_locale: str | None = Field(default=None, max_length=100)
    state: StoryState = StoryState.DRAFT
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class StoryParticipant(V3ContractModel):
    story_id: UUID
    participant_id: UUID
    sequence: int = Field(default=0, ge=0)
    role_label: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Scene(V3ContractModel):
    scene_id: UUID = Field(default_factory=uuid4)
    story_id: UUID
    sequence: int = Field(ge=0)
    title: str | None = Field(default=None, max_length=300)
    summary: str | None = Field(default=None, max_length=5000)
    setting: dict[str, Any] = Field(default_factory=dict)
    direction: dict[str, Any] = Field(default_factory=dict)
    duration_hint_ms: int | None = Field(default=None, ge=0)
    state: SceneState = SceneState.DRAFT
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class SceneParticipant(V3ContractModel):
    scene_id: UUID
    participant_id: UUID
    sequence: int = Field(default=0, ge=0)
    role_label: str | None = Field(default=None, max_length=200)
    placement: dict[str, Any] = Field(default_factory=dict)
    performance: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DialogueTurn(V3ContractModel):
    turn_id: UUID = Field(default_factory=uuid4)
    scene_id: UUID
    sequence: int = Field(ge=0)
    kind: DialogueTurnKind = DialogueTurnKind.SPEECH
    speaker_participant_id: UUID | None = None
    text: str = Field(min_length=1, max_length=20000)
    locale: str | None = Field(default=None, max_length=100)
    emotion_code: str | None = Field(default=None, max_length=200)
    delivery: dict[str, Any] = Field(default_factory=dict)
    start_offset_ms: int | None = Field(default=None, ge=0)
    duration_hint_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def validate_speaker(self) -> "DialogueTurn":
        if self.kind == DialogueTurnKind.SPEECH and self.speaker_participant_id is None:
            raise ValueError("speech_turn_requires_speaker_participant")
        return self


class StoryGraph(V3ContractModel):
    """Read/write graph for a complete story without denormalizing persistence."""

    project: Project
    participants: tuple[Participant, ...] = Field(min_length=1)
    story: Story
    story_participants: tuple[StoryParticipant, ...] = ()
    scenes: tuple[Scene, ...] = ()
    scene_participants: tuple[SceneParticipant, ...] = ()
    dialogue_turns: tuple[DialogueTurn, ...] = ()

    @model_validator(mode="after")
    def validate_graph(self) -> "StoryGraph":
        if self.project.account_id != self.story.account_id:
            raise ValueError("story_project_account_mismatch")
        if self.project.project_id != self.story.project_id:
            raise ValueError("story_project_id_mismatch")

        participant_ids = {p.participant_id for p in self.participants}
        if len(participant_ids) != len(self.participants):
            raise ValueError("duplicate_participant_id")
        for participant in self.participants:
            if participant.account_id != self.project.account_id:
                raise ValueError("participant_project_account_mismatch")
            if participant.project_id != self.project.project_id:
                raise ValueError("participant_project_id_mismatch")

        scene_ids = {s.scene_id for s in self.scenes}
        if len(scene_ids) != len(self.scenes):
            raise ValueError("duplicate_scene_id")
        sequences = [s.sequence for s in self.scenes]
        if len(set(sequences)) != len(sequences):
            raise ValueError("duplicate_scene_sequence")
        for scene in self.scenes:
            if scene.story_id != self.story.story_id:
                raise ValueError("scene_story_id_mismatch")

        for item in self.story_participants:
            if item.story_id != self.story.story_id or item.participant_id not in participant_ids:
                raise ValueError("invalid_story_participant_reference")
        for item in self.scene_participants:
            if item.scene_id not in scene_ids or item.participant_id not in participant_ids:
                raise ValueError("invalid_scene_participant_reference")
        for turn in self.dialogue_turns:
            if turn.scene_id not in scene_ids:
                raise ValueError("invalid_dialogue_scene_reference")
            if turn.speaker_participant_id is not None and turn.speaker_participant_id not in participant_ids:
                raise ValueError("invalid_dialogue_speaker_reference")

        return self

    @property
    def participant_count(self) -> int:
        return len(self.participants)
