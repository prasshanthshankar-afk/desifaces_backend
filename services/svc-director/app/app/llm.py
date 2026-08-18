from __future__ import annotations

import json
import os
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from df_contracts.v3.director import (
    CreativeBrief,
    CreativeCritique,
    CreativeStoryPlan,
    PlannedDialogueTurn,
    PlannedParticipant,
    PlannedScene,
)
from df_contracts.v3.story import DialogueTurnKind
from desifaces_shared.v3.participant_refs import normalize_participant_reference


_DIRECTOR_SYSTEM_PROMPT = """You are the desifaces Creative Director.
Create culturally aware, production-ready story plans without stereotyping people.
Preserve user intent, existing participant identity and continuity when context is supplied.
Never invent account IDs, media IDs, pricing, entitlements or provider capabilities.
Those are supplied and validated by deterministic tools outside the model.
For scene participant_refs and dialogue speaker_ref, use the exact participant display_name values.
For every person participant, make visual_direction sufficiently concrete for downstream Face Studio identity creation: include useful portrait framing, expression, hair/styling, lighting, and distinguishing visual cues when those are creatively appropriate. Preserve age or gender presentation only when explicitly supplied by the user/context. Do not infer ethnicity, skin tone, religion, attire, occupation, socioeconomic status, facial anatomy, or personality from geography, locale, name, or relationship role.
For flexible creative attributes, use concise key/value entries with plain-text values.
Use null when an optional scalar is unknown and [] when a list has no items.
Return only the requested structured schema.
"""

_CRITIC_SYSTEM_PROMPT = """You are the desifaces Creative Continuity Critic.
Evaluate story coherence, participant continuity, dialogue attribution, scene feasibility,
cultural sensitivity and whether the plan is sufficiently specified for downstream
Face, Audio and Fusion orchestration. For person participants, verify visual_direction
is useful for Face Studio identity creation without inventing protected/demographic
traits from geography, locale, names or relationship roles. Do not rewrite the story;
return a structured critique with actionable revision instructions. Use empty lists
when there are no issues.
"""


def _model_name() -> str:
    value = str(os.getenv("DF_DIRECTOR_LLM_MODEL") or "").strip()
    if not value:
        raise RuntimeError("DF_DIRECTOR_LLM_MODEL_required")
    return value


class _StrictWireModel(BaseModel):
    """OpenAI Structured Outputs-safe wire model.

    Canonical contracts intentionally allow open-ended dict metadata. Strict JSON
    schema generation should not expose those maps directly because Structured
    Outputs supports a constrained JSON Schema subset. The LLM therefore emits
    bounded key/value arrays, which are deterministically compiled into the richer
    canonical CreativeStoryPlan after validation.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _WireKV(_StrictWireModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=2000)


class _WireParticipant(_StrictWireModel):
    display_name: str = Field(min_length=1, max_length=200)
    role: str | None
    preferred_locale: str | None
    persona: list[_WireKV]
    continuity: list[_WireKV]
    visual_direction: list[_WireKV]
    voice_direction: list[_WireKV]


class _WireDialogueTurn(_StrictWireModel):
    sequence: int = Field(ge=0)
    kind: DialogueTurnKind
    speaker_ref: str | None
    text: str = Field(min_length=1, max_length=12000)
    locale: str | None
    emotion: str | None
    delivery: list[_WireKV]


class _WireScene(_StrictWireModel):
    sequence: int = Field(ge=0)
    title: str | None
    purpose: str | None
    participant_refs: list[str]
    setting: list[_WireKV]
    visual_direction: list[_WireKV]
    audio_direction: list[_WireKV]
    camera_direction: list[_WireKV]
    performance_direction: list[_WireKV]
    dialogue: list[_WireDialogueTurn]


class _WireCreativeStoryPlan(_StrictWireModel):
    title: str = Field(min_length=1, max_length=300)
    logline: str | None
    summary: str | None
    participants: list[_WireParticipant] = Field(min_length=1)
    scenes: list[_WireScene] = Field(min_length=1)
    continuity_plan: list[_WireKV]
    creative_direction: list[_WireKV]
    retrieved_context_refs: list[str]
    assumptions: list[str]


class _WireCreativeCritique(_StrictWireModel):
    score: int = Field(ge=0, le=100)
    ready: bool
    issues: list[str]
    revision_instructions: list[str]
    continuity_issues: list[str]
    safety_notes: list[str]


def _kv(items: list[_WireKV]) -> dict[str, Any]:
    return {item.key: item.value for item in items}


def _to_canonical_plan(wire: _WireCreativeStoryPlan) -> CreativeStoryPlan:
    participant_names = tuple(item.display_name for item in wire.participants)

    return CreativeStoryPlan(
        title=wire.title,
        logline=wire.logline,
        summary=wire.summary,
        participants=tuple(
            PlannedParticipant(
                display_name=item.display_name,
                role=item.role,
                preferred_locale=item.preferred_locale,
                persona=_kv(item.persona),
                continuity=_kv(item.continuity),
                visual_direction=_kv(item.visual_direction),
                voice_direction=_kv(item.voice_direction),
            )
            for item in wire.participants
        ),
        scenes=tuple(
            PlannedScene(
                sequence=scene.sequence,
                title=scene.title,
                purpose=scene.purpose,
                participant_refs=tuple(
                    normalize_participant_reference(ref, participant_names)
                    for ref in scene.participant_refs
                ),
                setting=_kv(scene.setting),
                visual_direction=_kv(scene.visual_direction),
                audio_direction=_kv(scene.audio_direction),
                camera_direction=_kv(scene.camera_direction),
                performance_direction=_kv(scene.performance_direction),
                dialogue=tuple(
                    PlannedDialogueTurn(
                        sequence=turn.sequence,
                        kind=turn.kind,
                        speaker_ref=normalize_participant_reference(
                            turn.speaker_ref,
                            participant_names,
                        ),
                        text=turn.text,
                        locale=turn.locale,
                        emotion=turn.emotion,
                        delivery=_kv(turn.delivery),
                    )
                    for turn in scene.dialogue
                ),
            )
            for scene in wire.scenes
        ),
        continuity_plan=_kv(wire.continuity_plan),
        creative_direction=_kv(wire.creative_direction),
        retrieved_context_refs=tuple(wire.retrieved_context_refs),
        assumptions=tuple(wire.assumptions),
    )


class OpenAICreativePlanner:
    """Initial OpenAI provider adapter; graph/domain remain provider-neutral."""

    def __init__(self) -> None:
        # Do not send temperature/top_p here. GPT-5.6 is a reasoning model and
        # production defaults should remain API-compatible across reasoning modes.
        self._model = ChatOpenAI(model=_model_name())
        self._structured = self._model.with_structured_output(
            _WireCreativeStoryPlan,
            method="json_schema",
        )

    async def plan(
        self,
        *,
        brief: CreativeBrief,
        retrieved_context: dict[str, Any],
        current_plan: CreativeStoryPlan | None = None,
        revision_feedback: str | None = None,
    ) -> CreativeStoryPlan:
        payload = {
            "creative_brief": brief.model_dump(mode="json"),
            "retrieved_context": retrieved_context,
            "current_plan": current_plan.model_dump(mode="json") if current_plan else None,
            "revision_feedback": revision_feedback,
        }
        result = await self._structured.ainvoke(
            [
                ("system", _DIRECTOR_SYSTEM_PROMPT),
                ("human", json.dumps(payload, ensure_ascii=False, default=str)),
            ]
        )
        wire = result if isinstance(result, _WireCreativeStoryPlan) else _WireCreativeStoryPlan.model_validate(result)
        return _to_canonical_plan(wire)


class OpenAICreativeCritic:
    def __init__(self) -> None:
        self._model = ChatOpenAI(model=_model_name())
        self._structured = self._model.with_structured_output(
            _WireCreativeCritique,
            method="json_schema",
        )

    async def critique(
        self,
        *,
        brief: CreativeBrief,
        plan: CreativeStoryPlan,
        retrieved_context: dict[str, Any],
    ) -> CreativeCritique:
        payload = {
            "creative_brief": brief.model_dump(mode="json"),
            "story_plan": plan.model_dump(mode="json"),
            "retrieved_context": retrieved_context,
        }
        result = await self._structured.ainvoke(
            [
                ("system", _CRITIC_SYSTEM_PROMPT),
                ("human", json.dumps(payload, ensure_ascii=False, default=str)),
            ]
        )
        wire = result if isinstance(result, _WireCreativeCritique) else _WireCreativeCritique.model_validate(result)
        return CreativeCritique(
            score=wire.score,
            ready=wire.ready,
            issues=tuple(wire.issues),
            revision_instructions=tuple(wire.revision_instructions),
            continuity_issues=tuple(wire.continuity_issues),
            safety_notes=tuple(wire.safety_notes),
        )
