from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from df_contracts.v3.director import (
    CreationContextBundle,
    CreationContextScope,
    DirectorRunState,
    StoryWorkspaceView,
    WorkspaceDialogueView,
    WorkspaceParticipantView,
    WorkspaceSceneView,
)
from df_contracts.v3.story import StoryGraph


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_story_workspace(
    graph: StoryGraph,
    *,
    revision: int = 1,
    director_state: DirectorRunState | None = None,
    active_scene_id: UUID | None = None,
    generation_states: Mapping[str, str] | None = None,
    media_by_scene: Mapping[UUID, Mapping[str, UUID]] | None = None,
    audio_by_turn: Mapping[UUID, UUID] | None = None,
    final_media_id: UUID | None = None,
    warnings: Sequence[str] = (),
    actions: Sequence[str] = (),
) -> StoryWorkspaceView:
    """Project canonical StoryGraph into a stable UI editing/read model."""

    states = dict(generation_states or {})
    media_map = dict(media_by_scene or {})
    audio_map = dict(audio_by_turn or {})
    participants = {p.participant_id: p for p in graph.participants}

    participant_views = tuple(
        WorkspaceParticipantView(
            participant_id=p.participant_id,
            display_name=p.display_name,
            kind=p.kind,
            primary_face_media_id=p.primary_face_media_id,
            voice_profile_ref=p.voice_profile_ref,
            preferred_locale=p.voice_locale or p.default_locale,
            persona=p.persona,
            continuity=p.continuity,
            generation_state=states.get(f"participant:{p.participant_id}"),
        )
        for p in graph.participants
    )

    scene_participants: dict[UUID, list[UUID]] = {}
    for membership in sorted(graph.scene_participants, key=lambda x: (str(x.scene_id), x.sequence)):
        scene_participants.setdefault(membership.scene_id, []).append(membership.participant_id)

    dialogue_by_scene: dict[UUID, list[WorkspaceDialogueView]] = {}
    for turn in sorted(graph.dialogue_turns, key=lambda x: (str(x.scene_id), x.sequence)):
        speaker = participants.get(turn.speaker_participant_id) if turn.speaker_participant_id else None
        dialogue_by_scene.setdefault(turn.scene_id, []).append(
            WorkspaceDialogueView(
                dialogue_turn_id=turn.turn_id,
                sequence=turn.sequence,
                kind=turn.kind,
                speaker_participant_id=turn.speaker_participant_id,
                speaker_display_name=speaker.display_name if speaker else None,
                text=turn.text,
                locale=turn.locale,
                emotion=turn.emotion_code,
                audio_media_id=audio_map.get(turn.turn_id),
                generation_state=states.get(f"turn:{turn.turn_id}"),
            )
        )

    scene_views = []
    for scene in sorted(graph.scenes, key=lambda x: x.sequence):
        scene_media = media_map.get(scene.scene_id, {})
        scene_views.append(
            WorkspaceSceneView(
                scene_id=scene.scene_id,
                sequence=scene.sequence,
                title=scene.title,
                participant_ids=tuple(scene_participants.get(scene.scene_id, ())),
                setting=scene.setting,
                visual_direction=dict(scene.direction.get("visual") or scene.direction),
                camera_direction=dict(scene.direction.get("camera") or {}),
                dialogue=tuple(dialogue_by_scene.get(scene.scene_id, ())),
                preview_media_id=scene_media.get("preview"),
                final_media_id=scene_media.get("final"),
                generation_state=states.get(f"scene:{scene.scene_id}") or scene.state.value,
            )
        )

    return StoryWorkspaceView(
        project_id=graph.project.project_id,
        story_id=graph.story.story_id,
        title=graph.story.title,
        status=graph.story.state.value,
        revision=revision,
        director_state=director_state,
        active_scene_id=active_scene_id,
        participants=participant_views,
        scenes=tuple(scene_views),
        final_media_id=final_media_id,
        warnings=tuple(warnings),
        actions=tuple(actions),
        updated_at=graph.story.updated_at,
    )


def build_creation_context(
    graph: StoryGraph,
    *,
    active_scene_id: UUID | None = None,
    active_participant_id: UUID | None = None,
    generation_context: Sequence[Mapping[str, Any]] = (),
    media_context: Sequence[Mapping[str, Any]] = (),
    pricing_context: Mapping[str, Any] | None = None,
    retrieved_context_refs: Sequence[str] = (),
    allowed_assistant_actions: Sequence[str] = (),
) -> CreationContextBundle:
    """Build a focus-aware grounded context bundle for the Assistant.

    Story scope includes the complete creation. Scene scope narrows dialogue and
    direction to one scene plus the participants appearing there. Participant
    scope narrows participant details while retaining summaries of scenes where
    that participant appears. No hidden prompts or chain-of-thought are exposed.
    """

    participants = {p.participant_id: p for p in graph.participants}
    scenes = {s.scene_id: s for s in graph.scenes}
    if active_scene_id is not None and active_scene_id not in scenes:
        raise ValueError(f"assistant_context_scene_not_found:{active_scene_id}")
    if active_participant_id is not None and active_participant_id not in participants:
        raise ValueError(f"assistant_context_participant_not_found:{active_participant_id}")

    scene_member_ids: dict[UUID, list[UUID]] = {}
    participant_scene_ids: dict[UUID, set[UUID]] = {}
    for membership in graph.scene_participants:
        scene_member_ids.setdefault(membership.scene_id, []).append(membership.participant_id)
        participant_scene_ids.setdefault(membership.participant_id, set()).add(membership.scene_id)

    if active_scene_id is not None:
        selected_scene_ids = {active_scene_id}
        selected_participant_ids = set(scene_member_ids.get(active_scene_id, ()))
        if active_participant_id is not None:
            if active_participant_id not in selected_participant_ids:
                raise ValueError(
                    f"assistant_context_participant_not_in_scene:{active_scene_id}:{active_participant_id}"
                )
            scope = CreationContextScope.SCENE_PARTICIPANT
        else:
            scope = CreationContextScope.SCENE
    elif active_participant_id is not None:
        selected_scene_ids = set(participant_scene_ids.get(active_participant_id, set()))
        selected_participant_ids = {active_participant_id}
        scope = CreationContextScope.PARTICIPANT
    else:
        selected_scene_ids = set(scenes)
        selected_participant_ids = set(participants)
        scope = CreationContextScope.STORY

    # Speakers are included even if historical data lacks an explicit scene
    # membership row. Canonical write paths normally keep both in sync.
    for turn in graph.dialogue_turns:
        if turn.scene_id in selected_scene_ids and turn.speaker_participant_id is not None:
            if scope in {CreationContextScope.STORY, CreationContextScope.SCENE}:
                selected_participant_ids.add(turn.speaker_participant_id)

    participant_context = tuple(
        {
            "participant_id": str(p.participant_id),
            "display_name": p.display_name,
            "kind": p.kind.value,
            "primary_face_media_id": str(p.primary_face_media_id) if p.primary_face_media_id else None,
            "voice_profile_ref": p.voice_profile_ref,
            "locale": p.voice_locale or p.default_locale,
            "persona": p.persona,
            "continuity": p.continuity,
        }
        for p in graph.participants
        if p.participant_id in selected_participant_ids
    )

    scene_context = tuple(
        {
            "scene_id": str(s.scene_id),
            "sequence": s.sequence,
            "title": s.title,
            "summary": s.summary,
            "setting": s.setting,
            "direction": s.direction,
            "state": s.state.value,
        }
        for s in sorted(graph.scenes, key=lambda x: x.sequence)
        if s.scene_id in selected_scene_ids
    )

    dialogue_context = tuple(
        {
            "turn_id": str(t.turn_id),
            "scene_id": str(t.scene_id),
            "sequence": t.sequence,
            "kind": t.kind.value,
            "speaker_participant_id": str(t.speaker_participant_id) if t.speaker_participant_id else None,
            "speaker_display_name": (
                participants[t.speaker_participant_id].display_name
                if t.speaker_participant_id in participants
                else None
            ),
            "text": t.text,
            "locale": t.locale,
            "emotion": t.emotion_code,
        }
        for t in sorted(graph.dialogue_turns, key=lambda x: (str(x.scene_id), x.sequence))
        if t.scene_id in selected_scene_ids
    )

    continuity_context = {
        str(p.participant_id): p.continuity
        for p in graph.participants
        if p.participant_id in selected_participant_ids and p.continuity
    }
    summary = graph.story.synopsis or (
        f"{graph.story.title}: {len(graph.participants)} participant(s), "
        f"{len(graph.scenes)} scene(s), {len(graph.dialogue_turns)} dialogue/action turn(s)."
    )

    return CreationContextBundle(
        account_id=graph.project.account_id,
        project_id=graph.project.project_id,
        story_id=graph.story.story_id,
        active_scene_id=active_scene_id,
        active_participant_id=active_participant_id,
        context_scope=scope,
        participant_ids=tuple(
            p.participant_id for p in graph.participants if p.participant_id in selected_participant_ids
        ),
        creation_type="story",
        title=graph.story.title,
        concise_summary=summary,
        participant_context=participant_context,
        scene_context=scene_context,
        dialogue_context=dialogue_context,
        continuity_context=continuity_context,
        generation_context=tuple(dict(x) for x in generation_context),
        media_context=tuple(dict(x) for x in media_context),
        pricing_context=dict(pricing_context or {}),
        retrieved_context_refs=tuple(retrieved_context_refs),
        allowed_assistant_actions=tuple(allowed_assistant_actions),
        generated_at=_now(),
    )
