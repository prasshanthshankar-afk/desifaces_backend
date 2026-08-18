from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from df_contracts.v3.director import (
    CreationContextBundle,
    CreativeBrief,
    DirectorRunState,
    DirectorRunView,
    StoryWorkspaceView,
)
from desifaces_shared.v3.creation_context import build_creation_context, build_story_workspace
from desifaces_shared.v3.director_graph import CreativeDirectorRuntime, build_creative_director_graph
from desifaces_shared.v3.story_store import CanonicalStoryStore, StoryGraphNotFound

from .compiler import CanonicalStoryCompiler
from .config import settings
from .db import close_pools, open_business_pool, open_checkpoint_pool
from .llm import OpenAICreativeCritic, OpenAICreativePlanner
from .retrieval import HybridCreativeRetriever
from .security import DirectorAuthContext, get_director_auth


class ResumeIn(BaseModel):
    approved: bool
    feedback: str | None = Field(default=None, max_length=12000)


def _interrupt_payload(result: dict) -> dict | None:
    raw = result.get("__interrupt__") if isinstance(result, dict) else None
    if not raw:
        return None
    first = raw[0] if isinstance(raw, (list, tuple)) else raw
    value = getattr(first, "value", None)
    if isinstance(value, dict):
        return value
    return {"value": value if value is not None else str(first)}


def _view(thread_id: str, result: dict) -> DirectorRunView:
    interrupt_payload = _interrupt_payload(result)
    workspace_raw = result.get("workspace")
    workspace = StoryWorkspaceView.model_validate(workspace_raw) if workspace_raw else None
    assistant_raw = result.get("assistant_context")
    assistant_context = CreationContextBundle.model_validate(assistant_raw) if assistant_raw else None
    phase = DirectorRunState.AWAITING_REVIEW if interrupt_payload else DirectorRunState(
        str(result.get("phase") or DirectorRunState.DRAFTING.value)
    )
    run_id_raw = result.get("run_id") or uuid4()
    return DirectorRunView(
        run_id=UUID(str(run_id_raw)),
        thread_id=thread_id,
        state=phase,
        project_id=workspace.project_id if workspace else None,
        story_id=workspace.story_id if workspace else None,
        workspace=workspace,
        assistant_context=assistant_context,
        interrupt=interrupt_payload,
        errors=tuple(str(x) for x in result.get("errors", ())),
    )


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
    app.state.director_graph = None
    app.state.director_config_error = None

    if settings.DF_DIRECTOR_LLM_MODEL:
        try:
            runtime = CreativeDirectorRuntime(
                retriever=HybridCreativeRetriever(business_pool),
                planner=OpenAICreativePlanner(),
                critic=OpenAICreativeCritic(),
                compiler=CanonicalStoryCompiler(business_pool),
                require_human_review=settings.DF_DIRECTOR_REVIEW_REQUIRED,
                max_revisions=max(0, settings.DF_DIRECTOR_MAX_REVISIONS),
            )
            app.state.director_graph = build_creative_director_graph(runtime, checkpointer=checkpointer)
        except Exception as exc:
            app.state.director_config_error = str(exc)

    try:
        yield
    finally:
        await close_pools()


app = FastAPI(title="desifaces V3 Creative Director", version="3.0", lifespan=lifespan)


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "service": "svc-director",
        "langgraph_checkpoint": "postgres",
        "llm_configured": bool(settings.DF_DIRECTOR_LLM_MODEL),
        "embedding_configured": bool(settings.DF_DIRECTOR_EMBEDDING_MODEL),
        "review_required": settings.DF_DIRECTOR_REVIEW_REQUIRED,
        "runtime_ready": app.state.director_graph is not None,
        "configuration_error": app.state.director_config_error,
    }


def _graph():
    graph = app.state.director_graph
    if graph is None:
        raise HTTPException(status_code=503, detail="creative_director_llm_not_configured")
    return graph


@app.post("/api/director/runs", response_model=DirectorRunView)
async def create_run(
    brief: CreativeBrief,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    thread_id = str(uuid4())
    run_id = uuid4()
    result = await _graph().ainvoke(
        {
            "run_id": str(run_id),
            "thread_id": thread_id,
            "account_id": str(auth.account_id),
            "owner_user_id": str(auth.user_id),
            "phase": DirectorRunState.DRAFTING.value,
            "brief": brief.model_dump(mode="json"),
            "revision_count": 0,
            "errors": [],
        },
        {"configurable": {"thread_id": thread_id}},
    )
    return _view(thread_id, result)


@app.post("/api/director/runs/{thread_id}/resume", response_model=DirectorRunView)
async def resume_run(
    thread_id: str,
    body: ResumeIn,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    graph = _graph()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)
    values = dict(snapshot.values or {})
    if str(values.get("account_id") or "") != str(auth.account_id) or str(values.get("owner_user_id") or "") != str(auth.user_id):
        raise HTTPException(status_code=404, detail="director_run_not_found")
    result = await graph.ainvoke(
        Command(resume={"approved": body.approved, "feedback": body.feedback or ""}),
        config,
    )
    return _view(thread_id, result)


@app.get("/api/director/runs/{thread_id}", response_model=DirectorRunView)
async def get_run(
    thread_id: str,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    graph = _graph()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)
    values = dict(snapshot.values or {})
    if not values:
        raise HTTPException(status_code=404, detail="director_run_not_found")
    if str(values.get("account_id") or "") != str(auth.account_id) or str(values.get("owner_user_id") or "") != str(auth.user_id):
        raise HTTPException(status_code=404, detail="director_run_not_found")
    return _view(thread_id, values)


@app.get("/api/director/stories/{story_id}/workspace", response_model=StoryWorkspaceView)
async def get_story_workspace(
    story_id: UUID,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = app.state.business_pool
    try:
        async with pool.acquire() as conn:
            graph = await app.state.story_store.get_story_graph(conn, story_id=story_id, account_id=auth.account_id)
    except StoryGraphNotFound as exc:
        raise HTTPException(status_code=404, detail="story_not_found") from exc
    return build_story_workspace(
        graph,
        actions=("edit_story", "generate_faces", "generate_audio", "generate_scene", "ask_assistant"),
    )


@app.get("/api/director/stories/{story_id}/assistant-context", response_model=CreationContextBundle)
async def get_story_assistant_context(
    story_id: UUID,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = app.state.business_pool
    try:
        async with pool.acquire() as conn:
            graph = await app.state.story_store.get_story_graph(conn, story_id=story_id, account_id=auth.account_id)
    except StoryGraphNotFound as exc:
        raise HTTPException(status_code=404, detail="story_not_found") from exc
    return build_creation_context(
        graph,
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
