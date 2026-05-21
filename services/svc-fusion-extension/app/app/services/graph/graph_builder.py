from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

try:
    from langgraph.graph import END, START, StateGraph
except Exception as langgraph_import_error:  # pragma: no cover
    StateGraph = None  # type: ignore[assignment]
    START = "__start__"
    END = "__end__"
    _LANGGRAPH_IMPORT_ERROR = str(langgraph_import_error)
else:
    _LANGGRAPH_IMPORT_ERROR = None

try:  # Prefer the shared typed state if present.
    from .state import FusionGraphState  # type: ignore
except Exception:  # pragma: no cover
    FusionGraphState = Dict[str, Any]  # type: ignore[misc,assignment]

logger = logging.getLogger("story_graph_builder")

AsyncNode = Callable[[FusionGraphState], Awaitable[FusionGraphState]]


class FusionStoryGraphBuilder:
    """
    Storytelling orchestration graph builder for svc-fusion-extension.

    Design goals:
      - graph_builder.py stays thin: node implementation belongs elsewhere
      - parent pricing is owned only at the storytelling job layer
      - child renders are internal executions routed through svc-fusion
      - failure routing is explicit, not only at poll time
    """

    REQUIRED_NODES = [
        "load_inputs",
        "reserve_parent_pricing",
        "parse_video_direction",
        "build_shot_plan",
        "route_providers",
        "submit_anchor_shots",
        "submit_dynamic_insert_jobs",
        "poll_child_jobs",
        "compose_timeline",
        "persist_final_artifact",
        "commit_parent_pricing",
        "release_parent_pricing",
        "mark_complete",
    ]

    def __init__(self, *, nodes: Dict[str, AsyncNode]) -> None:
        self.nodes = nodes

    @staticmethod
    def _has_fatal_error(state: FusionGraphState) -> bool:
        if not isinstance(state, dict):
            return False
        if state.get("fatal_error"):
            return True
        return bool(state.get("error_code") or state.get("error_message") or state.get("last_error"))

    @staticmethod
    def _router_after_step(state: FusionGraphState, *, success_target: str) -> str:
        return "release_parent_pricing" if FusionStoryGraphBuilder._has_fatal_error(state) else success_target

    @staticmethod
    def _poll_router(state: FusionGraphState) -> str:
        if FusionStoryGraphBuilder._has_fatal_error(state):
            return "release_parent_pricing"
        if bool(state.get("all_children_complete")):
            return "compose_timeline"
        return "poll_child_jobs"

    @staticmethod
    def _compose_router(state: FusionGraphState) -> str:
        if FusionStoryGraphBuilder._has_fatal_error(state):
            return "release_parent_pricing"
        if state.get("composed_video_url") or bool(state.get("timeline_composed")):
            return "persist_final_artifact"
        return "release_parent_pricing"

    @staticmethod
    def _persist_router(state: FusionGraphState) -> str:
        if FusionStoryGraphBuilder._has_fatal_error(state):
            return "release_parent_pricing"
        if state.get("final_artifact_id") or state.get("final_video_url") or state.get("composed_video_url"):
            return "commit_parent_pricing"
        return "release_parent_pricing"

    @staticmethod
    def _commit_router(state: FusionGraphState) -> str:
        if FusionStoryGraphBuilder._has_fatal_error(state):
            # Default to release if commit failed before the business layer marked success.
            if bool(state.get("release_on_commit_failure", True)):
                return "release_parent_pricing"
        return "mark_complete"

    def build(self):
        if StateGraph is None:
            raise RuntimeError(
                f"LangGraph is not installed or import failed: {_LANGGRAPH_IMPORT_ERROR}. "
                "Install with `pip install -U langgraph`."
            )

        missing = [name for name in self.REQUIRED_NODES if name not in self.nodes]
        if missing:
            raise ValueError(f"Missing Fusion graph nodes: {', '.join(missing)}")

        graph = StateGraph(dict)

        for name in self.REQUIRED_NODES:
            graph.add_node(name, self.nodes[name])

        graph.add_edge(START, "load_inputs")

        graph.add_conditional_edges(
            "load_inputs",
            lambda state: self._router_after_step(state, success_target="reserve_parent_pricing"),
            {
                "reserve_parent_pricing": "reserve_parent_pricing",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "reserve_parent_pricing",
            lambda state: self._router_after_step(state, success_target="parse_video_direction"),
            {
                "parse_video_direction": "parse_video_direction",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "parse_video_direction",
            lambda state: self._router_after_step(state, success_target="build_shot_plan"),
            {
                "build_shot_plan": "build_shot_plan",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "build_shot_plan",
            lambda state: self._router_after_step(state, success_target="route_providers"),
            {
                "route_providers": "route_providers",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "route_providers",
            lambda state: self._router_after_step(state, success_target="submit_anchor_shots"),
            {
                "submit_anchor_shots": "submit_anchor_shots",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "submit_anchor_shots",
            lambda state: self._router_after_step(state, success_target="submit_dynamic_insert_jobs"),
            {
                "submit_dynamic_insert_jobs": "submit_dynamic_insert_jobs",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "submit_dynamic_insert_jobs",
            lambda state: self._router_after_step(state, success_target="poll_child_jobs"),
            {
                "poll_child_jobs": "poll_child_jobs",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "poll_child_jobs",
            self._poll_router,
            {
                "compose_timeline": "compose_timeline",
                "release_parent_pricing": "release_parent_pricing",
                "poll_child_jobs": "poll_child_jobs",
            },
        )

        graph.add_conditional_edges(
            "compose_timeline",
            self._compose_router,
            {
                "persist_final_artifact": "persist_final_artifact",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "persist_final_artifact",
            self._persist_router,
            {
                "commit_parent_pricing": "commit_parent_pricing",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_conditional_edges(
            "commit_parent_pricing",
            self._commit_router,
            {
                "mark_complete": "mark_complete",
                "release_parent_pricing": "release_parent_pricing",
            },
        )

        graph.add_edge("mark_complete", END)
        graph.add_edge("release_parent_pricing", END)

        compiled = graph.compile()
        logger.info("story_graph_compiled", extra={"nodes": self.REQUIRED_NODES})
        return compiled
