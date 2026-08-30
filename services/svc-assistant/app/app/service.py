from __future__ import annotations

from uuid import uuid4

from .context import ContextResolver
from .llm import AssistantLLM
from .privacy import RESTRICTED_RESPONSE, classify_restricted_request, guard_output, redact_sensitive_text
from .recent_stories import RecentStoryResolver, is_recent_story_query
from .retrieval import SafeKnowledgeRetriever
from .schemas import AssistantAction, AssistantChatIn, AssistantChatOut, AssistantPolicyView
from .security import AssistantAuthContext
from .session_store import SessionStore

_ACTION_LABELS = {
    "explain_creation": "Explain this workflow",
    "edit_story": "Review story",
    "edit_participant": "Review participant",
    "edit_dialogue": "Review dialogue",
    "generate_faces": "Continue to Face generation",
    "generate_audio": "Continue to Audio generation",
    "generate_scene": "Continue to scene generation",
    "check_price": "Check price",
}

_OPERATIONAL_CUES = (
    "status", "progress", "what happened", "is it ready", "is my", "did my",
    "complete", "completed", "failed", "failure", "processing", "running",
    "queued", "latest", "last generation", "most recent",
)
_GENERATION_CUES = ("generation", "video", "fusion", "face", "image", "audio", "voice", "tts")
_SUCCESS_STATES = {"ready", "succeeded", "success", "complete", "completed", "done"}
_FAILURE_STATES = {"failed", "failure", "error", "cancelled", "canceled"}


def _normalized(value: object) -> str:
    return str(value or "").strip()


def _is_generation_operational_query(message: str) -> bool:
    text = message.lower().strip()
    return any(cue in text for cue in _OPERATIONAL_CUES) and any(cue in text for cue in _GENERATION_CUES)


def _requested_generation_kind(message: str) -> str | None:
    text = message.lower()
    if any(cue in text for cue in ("audio", "voice", "tts")):
        return "audio"
    if any(cue in text for cue in ("face", "image")):
        return "face"
    if any(cue in text for cue in ("video", "fusion")):
        return "video"
    if any(cue in text for cue in ("multi-person", "multi person", "multiperson")):
        return "video"
    return None


def _generation_descriptor(item: dict) -> str:
    kind = _normalized(item.get("kind")).lower() or "generation"
    people_mode = _normalized(item.get("people_mode")).lower()
    if people_mode == "multi_person" and kind == "video":
        return "multi-person video generation"
    if people_mode == "multi_person" and kind == "face":
        return "multi-person Face generation"
    if people_mode == "multi_person" and kind == "audio":
        return "multi-person Audio generation"
    if kind == "video":
        return "video generation"
    if kind == "face":
        return "Face generation"
    if kind == "audio":
        return "Audio generation"
    return "generation"


def _select_generation(message: str, generation: list[dict]) -> tuple[dict | None, str | None]:
    text = message.lower()
    expected_kind = _requested_generation_kind(message)
    wants_multi = any(cue in text for cue in ("multi-person", "multi person", "multiperson"))
    for item in generation:
        if not isinstance(item, dict):
            continue
        if expected_kind and _normalized(item.get("kind")).lower() != expected_kind:
            continue
        if wants_multi and _normalized(item.get("people_mode")).lower() != "multi_person":
            continue
        return item, None
    if wants_multi and expected_kind == "video":
        return None, "multi-person video"
    if expected_kind:
        return None, expected_kind
    return None, "generation"


def _format_generation_status(item: dict) -> str:
    descriptor = _generation_descriptor(item)
    status = _normalized(item.get("status")) or "unknown"
    stage = _normalized(item.get("stage"))
    progress = _normalized(item.get("progress"))
    failure_code = _normalized(item.get("failure_code"))
    updated_at = _normalized(item.get("updated_at")) or _normalized(item.get("created_at"))
    final_output_available = bool(item.get("final_output_available"))
    retryable = bool(item.get("retryable"))
    status_lower = status.lower()

    parts = [f"Your most recent {descriptor} is **{status}**."]
    if stage:
        parts.append(f"Current stage: **{stage}**.")
    if progress:
        parts.append(f"Progress: **{progress}**.")
    if _normalized(item.get("kind")).lower() == "video":
        if final_output_available:
            parts.append("Final video: **available**.")
        elif status_lower in _SUCCESS_STATES:
            parts.append("The generation is marked complete, but the final video is **not recorded as available yet**.")
        elif status_lower in _FAILURE_STATES:
            parts.append("Final video: **not available because this generation did not complete successfully**.")
        else:
            parts.append("Final video: **not available yet**.")
    if failure_code:
        parts.append(f"Failure code: **{failure_code}**.")
    if status_lower in _FAILURE_STATES:
        parts.append("Retry: **available**." if retryable else "Retry: **not indicated by the current generation state**.")
    elif retryable:
        parts.append("Retry: **available if needed**.")
    if updated_at:
        parts.append(f"Last updated: **{updated_at}**.")
    return " ".join(parts)


def operational_generation_answer(message: str, context: dict) -> str | None:
    if not _is_generation_operational_query(message):
        return None
    generation = [item for item in list(context.get("generation") or ()) if isinstance(item, dict)]
    item, requested_label = _select_generation(message, generation)
    if item is not None:
        return _format_generation_status(item)
    if context.get("live_context_available") is False:
        return "I checked your authenticated desifaces context, but live generation history is unavailable right now. Please try again shortly."
    if requested_label and requested_label != "generation":
        return f"I checked your current authenticated generation history and do not see a recent **{requested_label}** generation for this account."
    return "I checked your current authenticated generation history and do not see any recent generation records for this account."


class AssistantService:
    def __init__(
        self,
        *,
        sessions: SessionStore,
        context_resolver: ContextResolver,
        recent_stories: RecentStoryResolver,
        retriever: SafeKnowledgeRetriever,
        llm: AssistantLLM,
    ) -> None:
        self._sessions = sessions
        self._context_resolver = context_resolver
        self._recent_stories = recent_stories
        self._retriever = retriever
        self._llm = llm

    @staticmethod
    def _actions(context: dict) -> list[AssistantAction]:
        actions = []
        for action in list(context.get("allowed_actions") or ())[:5]:
            label = _ACTION_LABELS.get(str(action), str(action).replace("_", " ").title())
            actions.append(AssistantAction(type=str(action), label=label, requires_confirmation=True))
        return actions

    @staticmethod
    def _context_view(context: dict, body: AssistantChatIn) -> dict:
        return {
            "surface": context.get("surface", body.context.surface),
            "screen": context.get("screen", body.context.screen),
            "creation_type": context.get("creation_type"),
            "context_scope": context.get("context_scope"),
            "live_context_available": context.get("live_context_available"),
        }

    async def _store_exchange(self, *, auth: AssistantAuthContext, session_id, user_content: str, assistant_content: str) -> None:
        await self._sessions.append(
            account_id=auth.account_id, user_id=auth.user_id, session_id=session_id,
            role="user", content=user_content,
        )
        await self._sessions.append(
            account_id=auth.account_id, user_id=auth.user_id, session_id=session_id,
            role="assistant", content=assistant_content,
        )

    async def _recent_story_response(
        self, *, safe_message: str, body: AssistantChatIn, auth: AssistantAuthContext,
        session_id, message_id, redacted: bool,
    ) -> AssistantChatOut | None:
        if not is_recent_story_query(safe_message):
            return None
        stories = await self._recent_stories.recent(token=auth.token, limit=5)
        if not stories:
            answer = "I checked your authenticated Multi-Person history and do not see a resumable story yet. Start a new Multi-Person story when you are ready."
            actions: list[AssistantAction] = [
                AssistantAction(type="start_multi_person", label="Start multi-person story", requires_confirmation=False, href="/app/multi-person")
            ]
        else:
            story = stories[0]
            answer = f"Your most recent Multi-Person story is **{story.label}**. Its current Director state is **{story.state}**. You do not need to remember or copy a Story ID—use **Continue story** and desifaces will reopen the authoritative Director workflow."
            if story.updated_at:
                answer += f" Last updated: **{story.updated_at}**."
            actions = [
                AssistantAction(type="continue_story", label="Continue story", requires_confirmation=False, href=story.continue_path)
            ]
        guarded_answer, output_blocked = guard_output(answer)
        await self._store_exchange(
            auth=auth, session_id=session_id, user_content=safe_message, assistant_content=guarded_answer,
        )
        return AssistantChatOut(
            session_id=session_id,
            message_id=message_id,
            answer=guarded_answer,
            context={"surface": body.context.surface, "screen": body.context.screen, "context_scope": "director_recent_story"},
            suggested_actions=actions,
            policy=AssistantPolicyView(
                restricted=output_blocked,
                category="output_guard" if output_blocked else None,
                redacted=redacted or output_blocked,
            ),
            sources=[],
        )

    async def chat(self, body: AssistantChatIn, auth: AssistantAuthContext) -> AssistantChatOut:
        session_id = body.session_id or uuid4()
        message_id = uuid4()

        redacted = redact_sensitive_text(body.message)
        decision = classify_restricted_request(body.message)
        if decision.restricted:
            await self._store_exchange(
                auth=auth, session_id=session_id,
                user_content=f"[RESTRICTED_REQUEST:{decision.category}]",
                assistant_content=RESTRICTED_RESPONSE,
            )
            return AssistantChatOut(
                session_id=session_id,
                message_id=message_id,
                answer=RESTRICTED_RESPONSE,
                context={"surface": body.context.surface, "screen": body.context.screen},
                suggested_actions=[],
                policy=AssistantPolicyView(
                    restricted=True, category=decision.category, redacted=redacted.redacted,
                ),
                sources=[],
            )

        safe_message = redacted.text
        history = await self._sessions.history(
            account_id=auth.account_id, user_id=auth.user_id, session_id=session_id,
        )

        recent_story = await self._recent_story_response(
            safe_message=safe_message, body=body, auth=auth,
            session_id=session_id, message_id=message_id, redacted=redacted.redacted,
        )
        if recent_story is not None:
            return recent_story

        context = await self._context_resolver.resolve(
            body.context, token=auth.token, user_id=auth.user_id,
        )
        operational_answer = operational_generation_answer(safe_message, context)
        if operational_answer is not None:
            guarded_answer, output_blocked = guard_output(operational_answer)
            await self._store_exchange(
                auth=auth, session_id=session_id, user_content=safe_message, assistant_content=guarded_answer,
            )
            return AssistantChatOut(
                session_id=session_id,
                message_id=message_id,
                answer=guarded_answer,
                context=self._context_view(context, body),
                suggested_actions=self._actions(context),
                policy=AssistantPolicyView(
                    restricted=output_blocked,
                    category="output_guard" if output_blocked else None,
                    redacted=redacted.redacted or output_blocked,
                ),
                sources=[],
            )

        knowledge = await self._retriever.retrieve(safe_message)
        answer = await self._llm.answer(
            message=safe_message, history=history, context=context, knowledge=knowledge,
        )
        guarded_answer, output_blocked = guard_output(answer)
        await self._store_exchange(
            auth=auth, session_id=session_id, user_content=safe_message, assistant_content=guarded_answer,
        )
        return AssistantChatOut(
            session_id=session_id,
            message_id=message_id,
            answer=guarded_answer,
            context=self._context_view(context, body),
            suggested_actions=self._actions(context),
            policy=AssistantPolicyView(
                restricted=output_blocked,
                category="output_guard" if output_blocked else None,
                redacted=redacted.redacted or output_blocked,
            ),
            sources=[chunk.ref for chunk in knowledge],
        )
