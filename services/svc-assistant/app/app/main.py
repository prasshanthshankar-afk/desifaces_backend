from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response, status
from redis.asyncio import Redis

from .config import settings
from .context import ContextResolver
from .db import close_pool, open_business_pool
from .llm import AssistantLLM
from .retrieval import SafeKnowledgeRetriever
from .schemas import AssistantChatIn, AssistantChatOut
from .security import AssistantAuthContext, get_assistant_auth
from .service import AssistantService
from .session_store import SessionStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await open_business_pool()
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    await redis.ping()
    http = httpx.AsyncClient(timeout=settings.DF_ASSISTANT_HTTP_TIMEOUT_SECONDS)
    retriever = SafeKnowledgeRetriever()
    llm = AssistantLLM()

    app.state.redis = redis
    app.state.http = http
    app.state.retriever = retriever
    app.state.llm = llm
    app.state.service = AssistantService(
        sessions=SessionStore(redis),
        context_resolver=ContextResolver(http, pool),
        retriever=retriever,
        llm=llm,
    )
    try:
        yield
    finally:
        await http.aclose()
        await redis.aclose()
        await close_pool()


app = FastAPI(title="desifaces V3 Assistant", version="3.0", lifespan=lifespan)


@app.get("/api/health")
async def health():
    redis_ok = False
    try:
        redis_ok = bool(await app.state.redis.ping())
    except Exception:
        redis_ok = False
    llm_ready = app.state.llm.configured
    return {
        "ok": redis_ok,
        "service": "svc-assistant",
        "display_name": settings.DF_ASSISTANT_DISPLAY_NAME,
        "mode": "context_safe_read_only",
        "llm_configured": llm_ready,
        "llm_configuration_error": app.state.llm.configuration_error,
        "embedding_configured": bool(settings.DF_ASSISTANT_EMBEDDING_MODEL),
        "knowledge_chunks": app.state.retriever.chunk_count,
        "live_context": "dashboard+user_scoped_generation+director_story",
        "privacy_guard": "deterministic_pre_and_post_llm",
        "support_route": "support@desifaces.ai",
        "runtime_ready": redis_ok and llm_ready,
    }


@app.post("/api/assistant/chat", response_model=AssistantChatOut)
async def chat(
    body: AssistantChatIn,
    auth: AssistantAuthContext = Depends(get_assistant_auth),
):
    if not app.state.llm.configured:
        raise HTTPException(status_code=503, detail="assistant_llm_not_configured")
    try:
        return await app.state.service.chat(body, auth)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise HTTPException(status_code=403, detail="assistant_context_forbidden") from exc
        raise HTTPException(status_code=502, detail="assistant_context_unavailable") from exc


@app.delete("/api/assistant/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    auth: AssistantAuthContext = Depends(get_assistant_auth),
):
    sessions = SessionStore(app.state.redis)
    await sessions.delete(account_id=auth.account_id, user_id=auth.user_id, session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
