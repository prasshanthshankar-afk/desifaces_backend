from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from df_contracts.v3.director import CreativeBrief, CreativeCritique, CreativeStoryPlan, DirectorRunState
from df_contracts.v3.story import StoryGraph
from desifaces_shared.v3.creation_context import build_creation_context, build_story_workspace


class CreativeRetriever(Protocol):
    async def retrieve(self, *, brief: CreativeBrief, state: dict[str, Any]) -> dict[str, Any]: ...


class CreativePlanner(Protocol):
    async def plan(
        self,
        *,
        brief: CreativeBrief,
        retrieved_context: dict[str, Any],
        current_plan: CreativeStoryPlan | None = None,
        revision_feedback: str | None = None,
    ) -> CreativeStoryPlan: ...


class CreativeCritic(Protocol):
    async def critique(
        self,
        *,
        brief: CreativeBrief,
        plan: CreativeStoryPlan,
        retrieved_context: dict[str, Any],
    ) -> CreativeCritique: ...


class CreativeCompiler(Protocol):
    async def compile(
        self,
        *,
        brief: CreativeBrief,
        plan: CreativeStoryPlan,
        retrieved_context: dict[str, Any],
    ) -> StoryGraph: ...


class DirectorState(TypedDict, total=False):
    run_id: str
    thread_id: str
    phase: str
    brief: dict[str, Any]
    retrieved_context: dict[str, Any]
    retrieved_context_refs: list[str]
    plan: dict[str, Any]
    critique: dict[str, Any]
    revision_count: int
    review_approved: bool
    review_feedback: str
    story_graph: dict[str, Any]
    workspace: dict[str, Any]
    assistant_context: dict[str, Any]
    errors: list[str]


@dataclass(frozen=True)
class CreativeDirectorRuntime:
    retriever: CreativeRetriever
    planner: CreativePlanner
    critic: CreativeCritic
    compiler: CreativeCompiler
    require_human_review: bool = True
    max_revisions: int = 3


def build_creative_director_graph(runtime: CreativeDirectorRuntime, *, checkpointer=None):
    """Compile the shared LangGraph creative orchestration graph.

    Graph checkpoints are orchestration memory. Canonical StoryGraph persistence
    remains the business system of record and is written only by ``compiler``.
    """

    async def retrieve_context(state: DirectorState) -> DirectorState:
        brief = CreativeBrief.model_validate(state["brief"])
        context = await runtime.retriever.retrieve(brief=brief, state=dict(state))
        refs = [str(x) for x in context.get("refs", ())]
        return {
            "phase": DirectorRunState.RETRIEVING.value,
            "retrieved_context": context,
            "retrieved_context_refs": refs,
        }

    async def plan_story(state: DirectorState) -> DirectorState:
        brief = CreativeBrief.model_validate(state["brief"])
        plan = await runtime.planner.plan(
            brief=brief,
            retrieved_context=dict(state.get("retrieved_context") or {}),
        )
        return {
            "phase": DirectorRunState.PLANNING.value,
            "plan": plan.model_dump(mode="json"),
            "revision_count": int(state.get("revision_count") or 0),
        }

    async def critique_story(state: DirectorState) -> DirectorState:
        brief = CreativeBrief.model_validate(state["brief"])
        plan = CreativeStoryPlan.model_validate(state["plan"])
        critique = await runtime.critic.critique(
            brief=brief,
            plan=plan,
            retrieved_context=dict(state.get("retrieved_context") or {}),
        )
        return {
            "phase": DirectorRunState.CRITIQUING.value,
            "critique": critique.model_dump(mode="json"),
        }

    async def revise_story(state: DirectorState) -> DirectorState:
        brief = CreativeBrief.model_validate(state["brief"])
        plan = CreativeStoryPlan.model_validate(state["plan"])
        critique = CreativeCritique.model_validate(state.get("critique") or {"score": 0, "ready": False})
        feedback = state.get("review_feedback") or "\n".join(critique.revision_instructions or critique.issues)
        revised = await runtime.planner.plan(
            brief=brief,
            retrieved_context=dict(state.get("retrieved_context") or {}),
            current_plan=plan,
            revision_feedback=feedback or "Improve the plan while preserving the user's intent.",
        )
        return {
            "phase": DirectorRunState.PLANNING.value,
            "plan": revised.model_dump(mode="json"),
            "revision_count": int(state.get("revision_count") or 0) + 1,
            "review_feedback": "",
        }

    async def human_review(state: DirectorState) -> DirectorState:
        payload = {
            "type": "creative_plan_review",
            "run_id": state.get("run_id"),
            "thread_id": state.get("thread_id"),
            "plan": state.get("plan"),
            "critique": state.get("critique"),
            "revision_count": int(state.get("revision_count") or 0),
        }
        response = interrupt(payload)
        approved = bool((response or {}).get("approved")) if isinstance(response, dict) else bool(response)
        feedback = str((response or {}).get("feedback") or "") if isinstance(response, dict) else ""
        return {
            "phase": DirectorRunState.AWAITING_REVIEW.value,
            "review_approved": approved,
            "review_feedback": feedback,
        }

    async def compile_story(state: DirectorState) -> DirectorState:
        brief = CreativeBrief.model_validate(state["brief"])
        plan = CreativeStoryPlan.model_validate(state["plan"])
        graph = await runtime.compiler.compile(
            brief=brief,
            plan=plan,
            retrieved_context=dict(state.get("retrieved_context") or {}),
        )
        workspace = build_story_workspace(
            graph,
            revision=max(1, int(state.get("revision_count") or 0) + 1),
            director_state=DirectorRunState.READY,
            actions=("edit_story", "generate_faces", "generate_audio", "generate_scene", "ask_assistant"),
        )
        assistant_context = build_creation_context(
            graph,
            retrieved_context_refs=tuple(state.get("retrieved_context_refs") or ()),
            allowed_assistant_actions=(
                "explain_creation",
                "edit_story",
                "edit_participant",
                "edit_dialogue",
                "generate_faces",
                "generate_audio",
                "generate_scene",
                "check_price",
            ),
        )
        return {
            "phase": DirectorRunState.READY.value,
            "story_graph": graph.model_dump(mode="json"),
            "workspace": workspace.model_dump(mode="json"),
            "assistant_context": assistant_context.model_dump(mode="json"),
        }

    def after_critique(state: DirectorState) -> str:
        critique = CreativeCritique.model_validate(state["critique"])
        revisions = int(state.get("revision_count") or 0)
        if not critique.ready and revisions < runtime.max_revisions:
            return "revise"
        return "review" if runtime.require_human_review else "compile"

    def after_review(state: DirectorState) -> str:
        if bool(state.get("review_approved")):
            return "compile"
        return "revise"

    builder = StateGraph(DirectorState)
    builder.add_node("retrieve", retrieve_context)
    builder.add_node("plan", plan_story)
    builder.add_node("critique", critique_story)
    builder.add_node("revise", revise_story)
    builder.add_node("review", human_review)
    builder.add_node("compile", compile_story)

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "plan")
    builder.add_edge("plan", "critique")
    builder.add_conditional_edges("critique", after_critique, {"revise": "revise", "review": "review", "compile": "compile"})
    builder.add_edge("revise", "critique")
    builder.add_conditional_edges("review", after_review, {"compile": "compile", "revise": "revise"})
    builder.add_edge("compile", END)

    return builder.compile(checkpointer=checkpointer)
