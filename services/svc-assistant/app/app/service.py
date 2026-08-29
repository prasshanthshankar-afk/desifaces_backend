from __future__ import annotations

from uuid import uuid4

from .context import ContextResolver
from .llm import AssistantLLM
from .privacy import RESTRICTED_RESPONSE, classify_restricted_request, guard_output, redact_sensitive_text
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


class AssistantService:
    def __init__(
        self,
        *,
        sessions: SessionStore,
        context_resolver: ContextResolver,
        retriever: SafeKnowledgeRetriever,
        llm: AssistantLLM,
    ) -> None:
        self._sessions = sessions
        self._context_resolver = context_resolver
        self._retriever = retriever
        self._llm = llm

    @staticmethod
    def _actions(context: dict) -> list[AssistantAction]:
        actions = []
        for action in list(context.get("allowed_actions") or ())[:5]:
            label = _ACTION_LABELS.get(str(action), str(action).replace("_", " ").title())
            actions.append(AssistantAction(type=str(action), label=label, requires_confirmation=True))
        return actions

    async def chat(self, body: AssistantChatIn, auth: AssistantAuthContext) -> AssistantChatOut:
        session_id = body.session_id or uuid4()
        message_id = uuid4()

        redacted = redact_sensitive_text(body.message)
        decision = classify_restricted_request(body.message)
        if decision.restricted:
            await self._sessions.append(
                account_id=auth.account_id,
                user_id=auth.user_id,
                session_id=session_id,
                role="user",
                content=f"[RESTRICTED_REQUEST:{decision.category}]",
            )
            await self._sessions.append(
                account_id=auth.account_id,
                user_id=auth.user_id,
                session_id=session_id,
                role="assistant",
                content=RESTRICTED_RESPONSE,
            )
            return AssistantChatOut(
                session_id=session_id,
                message_id=message_id,
                answer=RESTRICTED_RESPONSE,
                context={"surface": body.context.surface, "screen": body.context.screen},
                suggested_actions=[],
                policy=AssistantPolicyView(
                    restricted=True,
                    category=decision.category,
                    redacted=redacted.redacted,
                ),
                sources=[],
            )

        safe_message = redacted.text
        history = await self._sessions.history(
            account_id=auth.account_id,
            user_id=auth.user_id,
            session_id=session_id,
        )
        context = await self._context_resolver.resolve(
            body.context,
            token=auth.token,
            user_id=auth.user_id,
        )
        knowledge = await self._retriever.retrieve(safe_message)
        answer = await self._llm.answer(
            message=safe_message,
            history=history,
            context=context,
            knowledge=knowledge,
        )
        guarded_answer, output_blocked = guard_output(answer)

        await self._sessions.append(
            account_id=auth.account_id,
            user_id=auth.user_id,
            session_id=session_id,
            role="user",
            content=safe_message,
        )
        await self._sessions.append(
            account_id=auth.account_id,
            user_id=auth.user_id,
            session_id=session_id,
            role="assistant",
            content=guarded_answer,
        )

        return AssistantChatOut(
            session_id=session_id,
            message_id=message_id,
            answer=guarded_answer,
            context={
                "surface": context.get("surface", body.context.surface),
                "screen": context.get("screen", body.context.screen),
                "creation_type": context.get("creation_type"),
                "context_scope": context.get("context_scope"),
                "live_context_available": context.get("live_context_available"),
            },
            suggested_actions=self._actions(context),
            policy=AssistantPolicyView(
                restricted=output_blocked,
                category="output_guard" if output_blocked else None,
                redacted=redacted.redacted or output_blocked,
            ),
            sources=[chunk.ref for chunk in knowledge],
        )
