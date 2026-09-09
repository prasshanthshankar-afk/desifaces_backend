from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, status
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import Error as PsycopgError
from pydantic import BaseModel, Field

from df_contracts.v3.director import (
    CreationContextBundle,
    CreativeBrief,
    DirectorRunState,
    DirectorRunView,
    StoryWorkspaceView,
)
from desifaces_shared.v3.creation_context import build_creation_context, build_story_workspace
from desifaces_shared.v3.story_store import CanonicalStoryStore, StoryGraphNotFound

from .config import settings
from .db import close_pools, open_business_pool, open_checkpoint_pool
from .face_pricing_context_runtime import install_director_face_pricing_context
from .run_store import DirectorRunNotFound, DirectorRunStore
from .runtime import create_director_graph
from .security import DirectorAuthContext, get_director_auth
from .studio_projection import load_story_studio_projection
from .studio_routes_runtime import router as studio_router


logger = logging.getLogger("svc-director")
_TRANSIENT_CHECKPOINT_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})


class ResumeIn(BaseModel):
    approved: bool
    feedback: str | None = Field(default=None, max_length=12000)


class RecentStoryView(BaseModel):
    story_id: UUID
    thread_id: str
    state: str
    title: str | None = None
    updated_at: datetime | None = None
    continue_path: str


def _coerce_interrupt_value(value: Any) -> dict | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    if isinstance(raw, dict):
        return raw
    return {"value": raw if isinstance(raw, (str, int, float, bool)) else str(raw)}


def _snapshot_interrupt(snapshot: Any) -> dict | None:
    for task in tuple(getattr(snapshot, "tasks", ()) or ()):
        for pending in tuple(getattr(task, "interrupts", ()) or ()):
            payload = _coerce_interrupt_value(pending)
            if payload:
                return payload
    return None


def _checkpoint_view(thread_id: str, values: dict, *, persisted_interrupt: dict | None = None) -> DirectorRunView:
    workspace_raw = values.get("workspace")
    workspace = StoryWorkspaceView.model_validate(workspace_raw) if workspace_raw else None
    assistant_raw = values.get("assistant_context")
    assistant_context = CreationContextBundle.model_validate(assistant_raw) if assistant_raw else None
    phase = DirectorRunState.AWAITING_REVIEW if persisted_interrupt else DirectorRunState(
        str(values.get("phase") or DirectorRunState.RUNNING.value)
    )
    return DirectorRunView(
        run_id=UUID(str(values["run_id"])), thread_id=thread_id, state=phase,
        project_id=workspace.project_id if workspace else None,
        story_id=workspace.story_id if workspace else None,
        workspace=workspace, assistant_context=assistant_context,
        interrupt=persisted_interrupt,
        errors=tuple(str(x) for x in values.get("errors", ())),
    )


def _queue_view(row) -> DirectorRunView:
    return DirectorRunView(
        run_id=UUID(str(row["run_id"])), thread_id=str(row["thread_id"]),
        state=DirectorRunState(str(row["state"])),
        project_id=UUID(str(row["project_id"])) if row["project_id"] else None,
        story_id=UUID(str(row["story_id"])) if row["story_id"] else None,
        errors=(str(row["last_error"]),) if row["last_error"] else (),
    )


def _is_transient_checkpoint_error(exc: BaseException) -> bool:
    if not isinstance(exc, PsycopgError):
        return False
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    return sqlstate.startswith("08") or sqlstate in _TRANSIENT_CHECKPOINT_SQLSTATES


async def _aget_state_resilient(graph: Any, config: dict[str, Any]):
    """Retry one transient checkpoint read after psycopg discards a dead connection."""
    try:
        return await graph.aget_state(config)
    except PsycopgError as exc:
        if not _is_transient_checkpoint_error(exc):
            raise
        logger.warning(
            "director_checkpoint_read_retry sqlstate=%s error=%s",
            getattr(exc, "sqlstate", None),
            type(exc).__name__,
        )
        # Give psycopg_pool a scheduling turn to discard the failed connection
        # before asking LangGraph for a fresh checkpoint connection.
        await asyncio.sleep(0.05)

    try:
        return await graph.aget_state(config)
    except PsycopgError as exc:
        if not _is_transient_checkpoint_error(exc):
            raise
        logger.error(
            "director_checkpoint_read_unavailable sqlstate=%s error=%s",
            getattr(exc, "sqlstate", None),
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="creative_director_state_temporarily_unavailable",
        ) from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    business_pool = await open_business_pool()
    checkpoint_pool = await open_checkpoint_pool()
    checkpointer = AsyncPostgresSaver(checkpoint_pool)
    if settings.DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP:
        await checkpointer.setup()
    app.state.business_pool = business_pool
    app.state.checkpointer = checkpointer
    app.state.story_store = CanonicalStoryStore()
    app.state.run_store = DirectorRunStore()
    app.state.director_graph = None
    app.state.director_config_error = None
    if settings.DF_DIRECTOR_LLM_MODEL:
        try:
            app.state.director_graph = create_director_graph(business_pool, checkpointer)
        except Exception as exc:
            app.state.director_config_error = str(exc)
    try:
        yield
    finally:
        await close_pools()


install_director_face_pricing_context()

app = FastAPI(title="desifaces V3 Creative Director", version="3.0", lifespan=lifespan)
app.include_router(studio_router)


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "service": "svc-director",
        "langgraph_checkpoint": "postgres",
        "execution_mode": "durable_queue",
        "llm_configured": bool(settings.DF_DIRECTOR_LLM_MODEL),
        "embedding_configured": bool(settings.DF_DIRECTOR_EMBEDDING_MODEL),
        "review_required": settings.DF_DIRECTOR_REVIEW_REQUIRED,
        "blocking_critic": settings.DF_DIRECTOR_BLOCKING_CRITIC,
        "max_revisions": max(0, settings.DF_DIRECTOR_MAX_REVISIONS),
        "runtime_ready": app.state.director_graph is not None,
        "configuration_error": app.state.director_config_error,
    }


def _graph():
    graph = app.state.director_graph
    if graph is None:
        raise HTTPException(status_code=503, detail="creative_director_llm_not_configured")
    return graph


@app.post("/api/director/runs", response_model=DirectorRunView, status_code=status.HTTP_202_ACCEPTED)
async def create_run(brief: CreativeBrief, auth: DirectorAuthContext = Depends(get_director_auth)):
    if app.state.director_graph is None:
        raise HTTPException(status_code=503, detail="creative_director_llm_not_configured")
    thread_id, run_id = str(uuid4()), uuid4()
    async with app.state.business_pool.acquire() as conn:
        await app.state.run_store.enqueue(
            conn, run_id=run_id, thread_id=thread_id, account_id=auth.account_id,
            owner_user_id=auth.user_id, brief=brief.model_dump(mode="json"),
        )
        row = await app.state.run_store.get(
            conn, thread_id=thread_id, account_id=auth.account_id, owner_user_id=auth.user_id,
        )
    return _queue_view(row)


@app.post("/api/director/runs/{thread_id}/resume", response_model=DirectorRunView, status_code=status.HTTP_202_ACCEPTED)
async def resume_run(thread_id: str, body: ResumeIn, auth: DirectorAuthContext = Depends(get_director_auth)):
    try:
        async with app.state.business_pool.acquire() as conn:
            row = await app.state.run_store.queue_resume(
                conn, thread_id=thread_id, account_id=auth.account_id, owner_user_id=auth.user_id,
                resume_payload={"approved": body.approved, "feedback": body.feedback or ""},
            )
    except DirectorRunNotFound as exc:
        raise HTTPException(status_code=409, detail="director_run_not_awaiting_review") from exc
    return _queue_view(row)


@app.get("/api/director/runs/{thread_id}", response_model=DirectorRunView)
async def get_run(thread_id: str, auth: DirectorAuthContext = Depends(get_director_auth)):
    try:
        async with app.state.business_pool.acquire() as conn:
            row = await app.state.run_store.get(
                conn, thread_id=thread_id, account_id=auth.account_id, owner_user_id=auth.user_id,
            )
    except DirectorRunNotFound as exc:
        raise HTTPException(status_code=404, detail="director_run_not_found") from exc
    db_state = str(row["state"])
    if db_state in {"queued", "running", "failed"}:
        return _queue_view(row)
    graph = _graph()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await _aget_state_resilient(graph, config)
    values = dict(snapshot.values or {})
    if not values:
        return _queue_view(row)
    return _checkpoint_view(thread_id, values, persisted_interrupt=_snapshot_interrupt(snapshot))


@app.get("/api/director/stories/recent", response_model=list[RecentStoryView])
async def recent_stories(
    limit: int = Query(default=10, ge=1, le=25),
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    async with app.state.business_pool.acquire() as conn:
        rows = await app.state.run_store.list_recent(
            conn, account_id=auth.account_id, owner_user_id=auth.user_id, limit=limit,
        )
    return [
        RecentStoryView(
            story_id=UUID(str(row["story_id"])),
            thread_id=str(row["thread_id"]),
            state=str(row["state"]),
            title=str(row["title"])[:160] if row["title"] else None,
            updated_at=row["updated_at"],
            continue_path=f"/app/multi-person?story={row['story_id']}",
        )
        for row in rows
    ]


@app.get("/api/director/stories/{story_id}/workspace", response_model=StoryWorkspaceView)
async def get_story_workspace(
    story_id: UUID, active_scene_id: UUID | None = None,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        async with app.state.business_pool.acquire() as conn:
            graph = await app.state.story_store.get_story_graph(conn, story_id=story_id, account_id=auth.account_id)
            if active_scene_id is not None and all(scene.scene_id != active_scene_id for scene in graph.scenes):
                raise HTTPException(status_code=404, detail="scene_not_found")
            states, _ = await load_story_studio_projection(
                conn, graph=graph, account_id=auth.account_id, active_scene_id=active_scene_id,
            )
    except StoryGraphNotFound as exc:
        raise HTTPException(status_code=404, detail="story_not_found") from exc
    return build_story_workspace(
        graph, active_scene_id=active_scene_id, generation_states=states,
        actions=("edit_story", "generate_faces", "generate_audio", "generate_scene", "ask_assistant"),
    )


@app.get("/api/director/stories/{story_id}/assistant-context", response_model=CreationContextBundle)
async def get_story_assistant_context(
    story_id: UUID, scene_id: UUID | None = None, participant_id: UUID | None = None,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    try:
        async with app.state.business_pool.acquire() as conn:
            graph = await app.state.story_store.get_story_graph(conn, story_id=story_id, account_id=auth.account_id)
            _, studio_context = await load_story_studio_projection(
                conn, graph=graph, account_id=auth.account_id,
                active_scene_id=scene_id, active_participant_id=participant_id,
            )
        return build_creation_context(
            graph,
            active_scene_id=scene_id,
            active_participant_id=participant_id,
            generation_context=studio_context,
            allowed_assistant_actions=(
                "explain_creation", "edit_story", "edit_participant", "edit_dialogue",
                "generate_faces", "generate_audio", "generate_scene", "check_price",
            ),
        )
    except StoryGraphNotFound as exc:
        raise HTTPException(status_code=404, detail="story_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
