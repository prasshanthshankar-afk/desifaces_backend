from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from df_contracts.v3.domain import GenerationKind, GenerationRequest, ParticipantKind, SafetyState
from df_contracts.v3.story import (
    DialogueTurn,
    DialogueTurnKind,
    Participant,
    Project,
    Scene,
    SceneParticipant,
    Story,
    StoryGraph,
    StoryParticipant,
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def make_graph(participant_count: int = 1) -> StoryGraph:
    account_id = uuid4()
    owner_user_id = uuid4()
    project_id = uuid4()
    story_id = uuid4()
    scene_id = uuid4()

    project = Project(
        project_id=project_id,
        account_id=account_id,
        owner_user_id=owner_user_id,
        title="Conversation",
        created_at=now(),
        updated_at=now(),
    )
    participants = tuple(
        Participant(
            participant_id=uuid4(),
            account_id=account_id,
            project_id=project_id,
            kind=ParticipantKind.PERSON,
            display_name=f"Person {index + 1}",
            created_at=now(),
            updated_at=now(),
        )
        for index in range(participant_count)
    )
    story = Story(
        story_id=story_id,
        account_id=account_id,
        project_id=project_id,
        title="A family conversation",
        created_at=now(),
        updated_at=now(),
    )
    scene = Scene(
        scene_id=scene_id,
        story_id=story_id,
        sequence=0,
        title="At home",
        setting={"location": "home"},
        created_at=now(),
        updated_at=now(),
    )
    memberships = tuple(
        StoryParticipant(story_id=story_id, participant_id=p.participant_id, sequence=index)
        for index, p in enumerate(participants)
    )
    scene_memberships = tuple(
        SceneParticipant(scene_id=scene_id, participant_id=p.participant_id, sequence=index)
        for index, p in enumerate(participants)
    )
    turns = tuple(
        DialogueTurn(
            scene_id=scene_id,
            sequence=index,
            kind=DialogueTurnKind.SPEECH,
            speaker_participant_id=p.participant_id,
            text=f"Line {index + 1}",
            created_at=now(),
        )
        for index, p in enumerate(participants)
    )
    return StoryGraph(
        project=project,
        participants=participants,
        story=story,
        story_participants=memberships,
        scenes=(scene,),
        scene_participants=scene_memberships,
        dialogue_turns=turns,
    )


def test_one_person_is_first_class_story_case() -> None:
    graph = make_graph(1)
    assert graph.participant_count == 1
    assert len(graph.dialogue_turns) == 1


def test_two_or_more_people_use_same_domain_model() -> None:
    graph = make_graph(4)
    assert graph.participant_count == 4
    assert len(graph.story_participants) == 4
    assert len(graph.scene_participants) == 4
    assert len(graph.dialogue_turns) == 4


def test_domain_does_not_encode_provider_subject_count_limit() -> None:
    # Provider-specific subject-count limits belong in capability/routing policy,
    # not in canonical Participant/Story contracts.
    graph = make_graph(20)
    assert graph.participant_count == 20


def test_speech_requires_durable_speaker_identity() -> None:
    with pytest.raises(ValidationError):
        DialogueTurn(
            scene_id=uuid4(),
            sequence=0,
            kind=DialogueTurnKind.SPEECH,
            text="Hello",
            created_at=now(),
        )


def test_narration_can_be_speakerless() -> None:
    turn = DialogueTurn(
        scene_id=uuid4(),
        sequence=0,
        kind=DialogueTurnKind.NARRATION,
        text="The evening begins.",
        created_at=now(),
    )
    assert turn.speaker_participant_id is None


def test_story_graph_rejects_cross_project_participant() -> None:
    graph = make_graph(1)
    bad = graph.participants[0].model_copy(update={"project_id": uuid4()})
    with pytest.raises(ValidationError):
        StoryGraph(
            project=graph.project,
            participants=(bad,),
            story=graph.story,
            story_participants=graph.story_participants,
            scenes=graph.scenes,
            scene_participants=graph.scene_participants,
            dialogue_turns=graph.dialogue_turns,
        )


def test_story_graph_rejects_duplicate_scene_sequence() -> None:
    graph = make_graph(1)
    second = Scene(
        story_id=graph.story.story_id,
        sequence=0,
        title="Duplicate sequence",
        created_at=now(),
        updated_at=now(),
    )
    payload = graph.model_dump()
    payload["scenes"] = [*payload["scenes"], second.model_dump()]
    with pytest.raises(ValidationError):
        StoryGraph.model_validate(payload)


def test_generation_request_links_story_scene_and_participants() -> None:
    graph = make_graph(2)
    request = GenerationRequest(
        account_id=graph.project.account_id,
        requested_by_user_id=graph.project.owner_user_id,
        project_id=graph.project.project_id,
        story_id=graph.story.story_id,
        scene_id=graph.scenes[0].scene_id,
        kind=GenerationKind.FUSION,
        participant_ids=tuple(p.participant_id for p in graph.participants),
        parameters={"active_speaker": str(graph.participants[0].participant_id)},
        safety_state=SafetyState.ALLOWED,
        created_at=now(),
    )
    assert request.story_id == graph.story.story_id
    assert request.scene_id == graph.scenes[0].scene_id
    assert len(request.participant_ids) == 2
