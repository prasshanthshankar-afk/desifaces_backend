from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response, status
from redis.asyncio import Redis

from .config import settings
from .context import ContextResolver
from .db import close_pool, open_business_pool
from .llm import AssistantLLM
from .recent_stories import RecentStoryResolver
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
        recent_stories=RecentStoryResolver(http),
        retriever=retriever,
        llm=llm,
    )
    try:
        yield
    finally:
        await http.aclose()
        await redis.aclose()
        await close_pool()


app = FastAPI(title="desifaces V3 Assistant", version="3.1", lifespan=lifespan)


async def _probe(url: str) -> bool:
    try:
        response = await app.state.http.get(url)
        if not response.is_success:
            return False
        payload = response.json()
        return bool(payload.get("ok", True)) if isinstance(payload, dict) else True
    except Exception:
        return False


async def _readiness() -> dict:
    redis_ok = False
    try:
        redis_ok = bool(await app.state.redis.ping())
    except Exception:
        redis_ok = False
    director_ok, dashboard_ok = await asyncio.gather(
        _probe(f"{settings.DF_DIRECTOR_BASE_URL}/api/health"),
        _probe(f"{settings.DF_DASHBOARD_BASE_URL}/api/health"),
    )
    llm_ok = bool(app.state.llm.configured)
    chat_ready = redis_ok and llm_ok and director_ok and dashboard_ok
    return {
        "ok": redis_ok,
        "service": "svc-assistant",
        "display_name": settings.DF_ASSISTANT_DISPLAY_NAME,
        "mode": "context_safe_read_only",
        "llm_configured": llm_ok,
        "embedding_configured": bool(settings.DF_ASSISTANT_EMBEDDING_MODEL),
        "knowledge_chunks": app.state.retriever.chunk_count,
        "live_context": "dashboard+user_scoped_generation+user_scoped_director_story",
        "privacy_guard": "deterministic_pre_and_post_llm",
        "support_route": "support@desifaces.ai",
        "redis_ready": redis_ok,
        "director_context_ready": director_ok,
        "dashboard_context_ready": dashboard_ok,
        "chat_ready": chat_ready,
        "runtime_ready": chat_ready,
    }


@app.get("/api/health")
async def health():
    return await _readiness()


@app.get("/api/ready")
async def ready():
    readiness = await _readiness()
    if not readiness["chat_ready"]:
        raise HTTPException(status_code=503, detail={
            "code": "assistant_not_ready",
            "redis_ready": readiness["redis_ready"],
            "llm_configured": readiness["llm_configured"],
            "director_context_ready": readiness["director_context_ready"],
            "dashboard_context_ready": readiness["dashboard_context_ready"],
        })
    return readiness


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
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="assistant_context_unavailable") from exc


@app.delete("/api/assistant/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    auth: AssistantAuthContext = Depends(get_assistant_auth),
):
    sessions = SessionStore(app.state.redis)
    await sessions.delete(account_id=auth.account_id, user_id=auth.user_id, session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
