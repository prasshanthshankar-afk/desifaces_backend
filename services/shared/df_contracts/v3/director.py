"""Creative Director contracts shared by orchestration, UI and Assistant.

The LLM never writes canonical persistence directly. It proposes a typed
``CreativeStoryPlan`` which is validated/compiled into the canonical StoryGraph.
The same persisted creation is then projected into ``StoryWorkspaceView`` for
UI clients and ``CreationContextBundle`` for conversational assistants.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from .common import V3ContractModel
from .domain import ParticipantKind
from .story import DialogueTurnKind


class DirectorRunState(StrEnum):
    DRAFTING = "drafting"
    RETRIEVING = "retrieving"
    PLANNING = "planning"
    CRITIQUING = "critiquing"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    COMPILING = "compiling"
    READY = "ready"
    FAILED = "failed"


class CreationContextScope(StrEnum):
    STORY = "story"
    SCENE = "scene"
    PARTICIPANT = "participant"
    SCENE_PARTICIPANT = "scene_participant"


class CreativeBrief(V3ContractModel):
    text: str = Field(min_length=1, max_length=12000)
    project_id: UUID | None = None
    story_id: UUID | None = None
    focus_scene_id: UUID | None = None
    focus_participant_id: UUID | None = None
    locale: str | None = Field(default=None, max_length=50)
    desired_duration_seconds: int | None = Field(default=None, ge=1, le=14400)
    desired_scene_count: int | None = Field(default=None, ge=1, le=200)
    participant_hints: tuple[dict[str, Any], ...] = ()
    constraints: dict[str, Any] = Field(default_factory=dict)


class PlannedParticipant(V3ContractModel):
    participant_id: UUID | None = None
    kind: ParticipantKind = ParticipantKind.PERSON
    display_name: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    persona: dict[str, Any] = Field(default_factory=dict)
    continuity: dict[str, Any] = Field(default_factory=dict)
    preferred_locale: str | None = Field(default=None, max_length=50)
    visual_direction: dict[str, Any] = Field(default_factory=dict)
    voice_direction: dict[str, Any] = Field(default_factory=dict)


class PlannedDialogueTurn(V3ContractModel):
    sequence: int = Field(ge=0)
    kind: DialogueTurnKind = DialogueTurnKind.SPEECH
    speaker_ref: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=12000)
    locale: str | None = Field(default=None, max_length=50)
    emotion: str | None = Field(default=None, max_length=100)
    delivery: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def speech_has_speaker(self):
        if self.kind == DialogueTurnKind.SPEECH and not self.speaker_ref:
            raise ValueError("speech_turn_requires_speaker_ref")
        return self


class PlannedScene(V3ContractModel):
    sequence: int = Field(ge=0)
    title: str | None = Field(default=None, max_length=300)
    purpose: str | None = Field(default=None, max_length=1000)
    participant_refs: tuple[str, ...] = ()
    setting: dict[str, Any] = Field(default_factory=dict)
    visual_direction: dict[str, Any] = Field(default_factory=dict)
    audio_direction: dict[str, Any] = Field(default_factory=dict)
    camera_direction: dict[str, Any] = Field(default_factory=dict)
    performance_direction: dict[str, Any] = Field(default_factory=dict)
    dialogue: tuple[PlannedDialogueTurn, ...] = ()


class CreativeStoryPlan(V3ContractModel):
    """Schema-constrained LLM proposal, never direct database truth."""

    title: str = Field(min_length=1, max_length=300)
    logline: str | None = Field(default=None, max_length=1500)
    summary: str | None = Field(default=None, max_length=6000)
    participants: tuple[PlannedParticipant, ...] = Field(min_length=1)
    scenes: tuple[PlannedScene, ...] = Field(min_length=1)
    continuity_plan: dict[str, Any] = Field(default_factory=dict)
    creative_direction: dict[str, Any] = Field(default_factory=dict)
    retrieved_context_refs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_references(self):
        names = [p.display_name for p in self.participants]
        if len(names) != len(set(names)):
            raise ValueError("planned_participant_display_names_must_be_unique")
        allowed = set(names)

        scene_sequences = [scene.sequence for scene in self.scenes]
        if len(scene_sequences) != len(set(scene_sequences)):
            raise ValueError("planned_scene_sequences_must_be_unique")

        for scene in self.scenes:
            if len(scene.participant_refs) != len(set(scene.participant_refs)):
                raise ValueError(f"scene_participant_refs_must_be_unique:{scene.sequence}")
            unknown = set(scene.participant_refs) - allowed
            if unknown:
                raise ValueError(f"scene_unknown_participant_refs:{sorted(unknown)}")

            turn_sequences = [turn.sequence for turn in scene.dialogue]
            if len(turn_sequences) != len(set(turn_sequences)):
                raise ValueError(f"dialogue_sequences_must_be_unique:{scene.sequence}")

            scene_people = set(scene.participant_refs)
            for turn in scene.dialogue:
                if turn.speaker_ref and turn.speaker_ref not in allowed:
                    raise ValueError(f"dialogue_unknown_speaker_ref:{turn.speaker_ref}")
                if turn.speaker_ref and turn.speaker_ref not in scene_people:
                    raise ValueError(
                        f"dialogue_speaker_not_in_scene:{scene.sequence}:{turn.speaker_ref}"
                    )
        return self


class CreativeCritique(V3ContractModel):
    score: int = Field(ge=0, le=100)
    ready: bool
    issues: tuple[str, ...] = ()
    revision_instructions: tuple[str, ...] = ()
    continuity_issues: tuple[str, ...] = ()
    safety_notes: tuple[str, ...] = ()


class WorkspaceParticipantView(V3ContractModel):
    participant_id: UUID
    display_name: str | None = None
    kind: ParticipantKind
    primary_face_media_id: UUID | None = None
    voice_profile_ref: str | None = None
    preferred_locale: str | None = None
    persona: dict[str, Any] = Field(default_factory=dict)
    continuity: dict[str, Any] = Field(default_factory=dict)
    generation_state: str | None = None


class WorkspaceDialogueView(V3ContractModel):
    dialogue_turn_id: UUID
    sequence: int
    kind: DialogueTurnKind
    speaker_participant_id: UUID | None = None
    speaker_display_name: str | None = None
    text: str
    locale: str | None = None
    emotion: str | None = None
    audio_media_id: UUID | None = None
    generation_state: str | None = None


class WorkspaceSceneView(V3ContractModel):
    scene_id: UUID
    sequence: int
    title: str | None = None
    participant_ids: tuple[UUID, ...] = ()
    setting: dict[str, Any] = Field(default_factory=dict)
    visual_direction: dict[str, Any] = Field(default_factory=dict)
    camera_direction: dict[str, Any] = Field(default_factory=dict)
    dialogue: tuple[WorkspaceDialogueView, ...] = ()
    preview_media_id: UUID | None = None
    final_media_id: UUID | None = None
    generation_state: str | None = None


class StoryWorkspaceView(V3ContractModel):
    """Stable, UI-oriented projection for Web/Mobile story editors."""

    project_id: UUID
    story_id: UUID
    title: str
    status: str
    revision: int = Field(ge=1)
    director_state: DirectorRunState | None = None
    active_scene_id: UUID | None = None
    participants: tuple[WorkspaceParticipantView, ...]
    scenes: tuple[WorkspaceSceneView, ...]
    final_media_id: UUID | None = None
    warnings: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    updated_at: datetime


class CreationContextBundle(V3ContractModel):
    """Assistant-ready grounded context for creation-specific responses/actions."""

    account_id: UUID
    project_id: UUID
    story_id: UUID | None = None
    active_scene_id: UUID | None = None
    active_participant_id: UUID | None = None
    context_scope: CreationContextScope = CreationContextScope.STORY
    participant_ids: tuple[UUID, ...] = ()
    creation_type: str
    title: str | None = None
    concise_summary: str = Field(max_length=6000)
    participant_context: tuple[dict[str, Any], ...] = ()
    scene_context: tuple[dict[str, Any], ...] = ()
    dialogue_context: tuple[dict[str, Any], ...] = ()
    continuity_context: dict[str, Any] = Field(default_factory=dict)
    generation_context: tuple[dict[str, Any], ...] = ()
    media_context: tuple[dict[str, Any], ...] = ()
    pricing_context: dict[str, Any] = Field(default_factory=dict)
    retrieved_context_refs: tuple[str, ...] = ()
    allowed_assistant_actions: tuple[str, ...] = ()
    context_version: str = "v3"
    generated_at: datetime


class DirectorRunView(V3ContractModel):
    run_id: UUID
    thread_id: str = Field(min_length=1, max_length=300)
    state: DirectorRunState
    project_id: UUID | None = None
    story_id: UUID | None = None
    workspace: StoryWorkspaceView | None = None
    assistant_context: CreationContextBundle | None = None
    interrupt: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()
