from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from df_contracts.v3.director import (
    CreationContextScope,
    CreativeBrief,
    CreativeCritique,
    CreativeStoryPlan,
    PlannedDialogueTurn,
    PlannedParticipant,
    PlannedScene,
)
from df_contracts.v3.domain import ParticipantKind
from df_contracts.v3.story import DialogueTurn, Participant, Project, Scene, SceneParticipant, Story, StoryGraph, StoryParticipant
from desifaces_shared.v3.creation_context import build_creation_context
from desifaces_shared.v3.director_graph import CreativeDirectorRuntime, build_creative_director_graph


def now():
    return datetime.now(timezone.utc)


class FakeRetriever:
    async def retrieve(self, *, brief, state):
        return {
            "refs": ["creative_chunk:test"],
            "structured": {
                "account_id": state["account_id"],
                "owner_user_id": state["owner_user_id"],
                "thread_id": state["thread_id"],
            },
            "creative_knowledge": [{"ref": "creative_chunk:test", "content": "grounded creative guidance"}],
            "thread_id": state["thread_id"],
        }


class FakePlanner:
    async def plan(self, *, brief, retrieved_context, current_plan=None, revision_feedback=None):
        return CreativeStoryPlan(
            title="A family conversation",
            summary="A daughter and her father talk warmly at home.",
            participants=(
                PlannedParticipant(display_name="Ananya", role="daughter", preferred_locale="ta-IN"),
                PlannedParticipant(display_name="Appa", role="father", preferred_locale="ta-IN"),
            ),
            scenes=(
                PlannedScene(
                    sequence=0,
                    title="At home",
                    participant_refs=("Ananya", "Appa"),
                    setting={"location": "home"},
                    camera_direction={"shot": "two-shot"},
                    dialogue=(
                        PlannedDialogueTurn(sequence=0, speaker_ref="Ananya", text="Appa, can we talk?", locale="ta-IN"),
                        PlannedDialogueTurn(sequence=1, speaker_ref="Appa", text="Of course.", locale="ta-IN"),
                    ),
                ),
            ),
            continuity_plan={"preserve_identity": True},
            retrieved_context_refs=("creative_chunk:test",),
        )


class FakeCritic:
    async def critique(self, *, brief, plan, retrieved_context):
        return CreativeCritique(score=95, ready=True)


class FakeCompiler:
    async def compile(self, *, brief, plan, retrieved_context):
        account_id = UUID(retrieved_context["structured"]["account_id"])
        owner_user_id = UUID(retrieved_context["structured"]["owner_user_id"])
        project_id = uuid4()
        story_id = uuid4()
        scene_id = uuid4()
        participants = tuple(
            Participant(
                participant_id=uuid4(),
                account_id=account_id,
                project_id=project_id,
                kind=ParticipantKind.PERSON,
                display_name=p.display_name,
                default_locale=p.preferred_locale,
                created_at=now(),
                updated_at=now(),
            )
            for p in plan.participants
        )
        by_name = {p.display_name: p for p in participants}
        return StoryGraph(
            project=Project(
                project_id=project_id,
                account_id=account_id,
                owner_user_id=owner_user_id,
                title=plan.title,
                created_at=now(),
                updated_at=now(),
            ),
            participants=participants,
            story=Story(
                story_id=story_id,
                account_id=account_id,
                project_id=project_id,
                title=plan.title,
                synopsis=plan.summary,
                created_at=now(),
                updated_at=now(),
            ),
            story_participants=tuple(
                StoryParticipant(story_id=story_id, participant_id=p.participant_id, sequence=i)
                for i, p in enumerate(participants)
            ),
            scenes=(Scene(scene_id=scene_id, story_id=story_id, sequence=0, title="At home", created_at=now(), updated_at=now()),),
            scene_participants=tuple(
                SceneParticipant(scene_id=scene_id, participant_id=p.participant_id, sequence=i)
                for i, p in enumerate(participants)
            ),
            dialogue_turns=tuple(
                DialogueTurn(
                    scene_id=scene_id,
                    sequence=i,
                    speaker_participant_id=by_name[t.speaker_ref].participant_id,
                    text=t.text,
                    locale=t.locale,
                    created_at=now(),
                )
                for i, t in enumerate(plan.scenes[0].dialogue)
            ),
        )


def _input(thread_id: str):
    return {
        "run_id": str(uuid4()),
        "thread_id": thread_id,
        "account_id": str(uuid4()),
        "owner_user_id": str(uuid4()),
        "brief": CreativeBrief(text="Create a two-person family conversation", locale="ta-IN").model_dump(mode="json"),
    }


def _run_director():
    async def run():
        runtime = CreativeDirectorRuntime(
            retriever=FakeRetriever(),
            planner=FakePlanner(),
            critic=FakeCritic(),
            compiler=FakeCompiler(),
            require_human_review=False,
        )
        graph = build_creative_director_graph(runtime, checkpointer=InMemorySaver())
        thread_id = "director-test-thread"
        return await graph.ainvoke(
            _input(thread_id),
            {"configurable": {"thread_id": thread_id}},
        )

    return asyncio.run(run())


def test_director_produces_canonical_ui_and_assistant_views_from_same_story():
    state = _run_director()
    canonical = state["story_graph"]
    workspace = state["workspace"]
    assistant = state["assistant_context"]
    assert state["phase"] == "ready"
    assert workspace["story_id"] == canonical["story"]["story_id"]
    assert assistant["story_id"] == canonical["story"]["story_id"]
    assert workspace["project_id"] == assistant["project_id"] == canonical["project"]["project_id"]
    assert set(assistant["participant_ids"]) == {p["participant_id"] for p in canonical["participants"]}
    assert workspace["scenes"][0]["dialogue"][0]["speaker_display_name"] == "Ananya"
    assert "ask_assistant" in workspace["actions"]
    assert "edit_dialogue" in assistant["allowed_assistant_actions"]
    assert "creative_chunk:test" in assistant["retrieved_context_refs"]


def test_assistant_context_can_focus_on_current_scene_and_participant():
    state = _run_director()
    graph = StoryGraph.model_validate(state["story_graph"])
    scene_id = graph.scenes[0].scene_id
    participant_id = graph.participants[0].participant_id
    context = build_creation_context(
        graph,
        active_scene_id=scene_id,
        active_participant_id=participant_id,
        allowed_assistant_actions=("edit_dialogue",),
    )
    assert context.context_scope is CreationContextScope.SCENE_PARTICIPANT
    assert context.active_scene_id == scene_id
    assert context.active_participant_id == participant_id
    assert {item["scene_id"] for item in context.dialogue_context} == {str(scene_id)}
    assert participant_id in context.participant_ids


def test_director_can_interrupt_for_review_and_resume_same_thread():
    async def run():
        runtime = CreativeDirectorRuntime(
            retriever=FakeRetriever(),
            planner=FakePlanner(),
            critic=FakeCritic(),
            compiler=FakeCompiler(),
            require_human_review=True,
        )
        graph = build_creative_director_graph(runtime, checkpointer=InMemorySaver())
        thread_id = "director-hitl-thread"
        config = {"configurable": {"thread_id": thread_id}}
        first = await graph.ainvoke(_input(thread_id), config)
        assert first.get("__interrupt__")
        snapshot = await graph.aget_state(config)
        assert snapshot.tasks
        assert any(getattr(task, "interrupts", ()) for task in snapshot.tasks)

        resumed = await graph.ainvoke(Command(resume={"approved": True, "feedback": ""}), config)
        assert resumed["phase"] == "ready"
        assert resumed["workspace"]["story_id"] == resumed["assistant_context"]["story_id"]

    asyncio.run(run())
