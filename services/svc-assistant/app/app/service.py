from __future__ import annotations

import re
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
    "open_saved_work": "Open Saved work",
    "open_plans_usage": "Open Plans & usage",
    "open_face_studio": "Open Face Studio",
    "open_voice_studio": "Open Voice Studio",
    "open_video_studio": "Open Video Studio",
    "open_multi_person": "Open Multi-Person",
}
_ACTION_HREFS = {
    "open_saved_work": "/app/library",
    "open_plans_usage": "/app/billing",
    "open_face_studio": "/app/face",
    "open_voice_studio": "/app/audio",
    "open_video_studio": "/app/video",
    "open_multi_person": "/app/multi-person",
}

_OPERATIONAL_CUES = (
    "status",
    "progress",
    "what happened",
    "is it ready",
    "is my",
    "did my",
    "complete",
    "completed",
    "failed",
    "failure",
    "processing",
    "running",
    "queued",
    "latest",
    "last generation",
    "most recent",
)
_GENERATION_CUES = (
    "generation",
    "video",
    "fusion",
    "face",
    "image",
    "audio",
    "voice",
    "tts",
)
_CREDIT_CUES = (
    "credit",
    "credits",
    "balance",
    "available",
    "afford",
    "enough",
    "how many",
    "can i create",
    "can i generate",
    "will i be able",
    "runway",
    "cost",
    "pricing",
)
_SUCCESS_STATES = {"ready", "succeeded", "success", "complete", "completed", "done"}
_FAILURE_STATES = {"failed", "failure", "error", "cancelled", "canceled"}


def _normalized(value: object) -> str:
    return str(value or "").strip()


def _as_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: object) -> str:
    number = _as_number(value)
    if number is None:
        return _normalized(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _is_generation_operational_query(message: str) -> bool:
    text = message.lower().strip()
    return any(cue in text for cue in _OPERATIONAL_CUES) and any(cue in text for cue in _GENERATION_CUES)


def _is_credit_query(message: str) -> bool:
    text = message.lower().strip()
    return any(cue in text for cue in _CREDIT_CUES)


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


def _requested_capacity_kinds(message: str) -> list[str]:
    text = message.lower()
    kinds: list[str] = []
    if any(cue in text for cue in ("face", "faces", "image", "images")):
        kinds.append("face")
    if any(cue in text for cue in ("audio", "audios", "voice", "voices", "tts")):
        kinds.append("audio")
    if any(cue in text for cue in ("video", "videos", "fusion")):
        kinds.append("video")
    return kinds


def _requested_count(message: str) -> int | None:
    match = re.search(r"\b(\d{1,5})\b", message)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


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
    """Return a database-backed operational answer when the question asks for live generation state."""
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


def _pricing_available_credits(pricing: dict) -> float | None:
    credits = pricing.get("credits") if isinstance(pricing.get("credits"), dict) else {}
    summary = pricing.get("summary") if isinstance(pricing.get("summary"), dict) else {}
    runway = pricing.get("runway") if isinstance(pricing.get("runway"), dict) else {}
    for value in (
        credits.get("total_available"),
        credits.get("available_credits"),
        credits.get("total_spendable"),
        summary.get("total_available"),
        summary.get("available_credits"),
        runway.get("available_credits"),
    ):
        number = _as_number(value)
        if number is not None:
            return number
    return None


def _pricing_reserved_credits(pricing: dict) -> float | None:
    credits = pricing.get("credits") if isinstance(pricing.get("credits"), dict) else {}
    summary = pricing.get("summary") if isinstance(pricing.get("summary"), dict) else {}
    runway = pricing.get("runway") if isinstance(pricing.get("runway"), dict) else {}
    for value in (
        credits.get("total_reserved"),
        credits.get("reserved_credits"),
        summary.get("total_reserved"),
        summary.get("reserved_credits"),
        runway.get("reserved_credits"),
    ):
        number = _as_number(value)
        if number is not None:
            return number
    return None


def _runway_kind(item: dict) -> str | None:
    studio = _normalized(item.get("studio")).lower()
    if "face" in studio or "image" in studio:
        return "face"
    if "audio" in studio or "tts" in studio or "voice" in studio:
        return "audio"
    if "video" in studio or "fusion" in studio or "longform" in studio:
        return "video"
    return None


def _runway_estimates(context: dict) -> list[dict]:
    pricing = context.get("pricing") if isinstance(context.get("pricing"), dict) else {}
    runway = pricing.get("runway") if isinstance(pricing.get("runway"), dict) else {}
    raw = runway.get("estimates")
    return [item for item in list(raw or ()) if isinstance(item, dict)]


def _select_runway_estimates(message: str, context: dict) -> list[dict]:
    requested = _requested_capacity_kinds(message)
    estimates = _runway_estimates(context)
    selected: list[dict] = []
    seen: set[str] = set()
    for item in estimates:
        kind = _runway_kind(item)
        if kind is None:
            continue
        if requested and kind not in requested:
            continue
        if kind in seen:
            continue
        selected.append(item)
        seen.add(kind)
    return selected


def _runway_line(item: dict, requested_count: int | None) -> tuple[str, bool | None]:
    kind = _runway_kind(item) or "generation"
    label = _normalized(item.get("label")) or _normalized(item.get("mode")) or kind.title()
    unit = _normalized(item.get("unit")) or "units"
    remaining = _as_number(item.get("remaining_units"))
    per_unit = _as_number(item.get("estimated_credits_per_display_unit"))
    baseline_qty = _as_number(item.get("baseline_display_qty"))
    baseline_cost = _as_number(item.get("estimated_credits_for_baseline_qty"))

    unit_label = unit
    if unit == "kchars":
        unit_label = "1K-character audio blocks"
    elif unit == "chars":
        unit_label = "audio characters"
    elif unit == "seconds":
        unit_label = "video seconds" if kind == "video" else "seconds"
    elif unit == "minutes":
        unit_label = "video minutes" if kind == "video" else "minutes"
    elif unit == "runs":
        unit_label = f"{kind} runs"

    details: list[str] = [f"**{kind.title()} — {label}:**"]
    if remaining is not None:
        details.append(f"about **{_format_number(remaining)} {unit_label}** available from the current balance")
    if per_unit is not None:
        details.append(f"at about **{_format_number(per_unit)} credits per {unit.rstrip('s') or 'unit'}**")
    elif baseline_cost is not None and baseline_qty is not None:
        details.append(
            f"with a baseline of **{_format_number(baseline_cost)} credits for {_format_number(baseline_qty)} {unit}**"
        )

    count_result: bool | None = None
    if requested_count is not None and unit == "runs" and remaining is not None:
        count_result = remaining >= requested_count
        details.append(
            f"so **{requested_count} {kind} generations {'fit' if count_result else 'do not fit'}** within that runway"
        )
    elif requested_count is not None and unit != "runs":
        details.append(
            f"{requested_count} complete {kind} items depend on each item's length, because this mode is priced by **{unit}**, not by item count"
        )

    return "; ".join(details) + ".", count_result


def operational_credit_answer(message: str, context: dict) -> str | None:
    """Answer balance/runway questions from authenticated pricing data, independent of current screen."""
    if not _is_credit_query(message):
        return None

    pricing = context.get("pricing") if isinstance(context.get("pricing"), dict) else {}
    available = _pricing_available_credits(pricing)
    reserved = _pricing_reserved_credits(pricing)
    plan = pricing.get("plan") if isinstance(pricing.get("plan"), dict) else {}
    plan_name = _normalized(plan.get("plan_name")) or _normalized(plan.get("plan_code"))

    if available is None:
        return (
            "I checked your authenticated desifaces pricing context, but the current credit balance is unavailable right now. "
            "I won't estimate a balance that the pricing system did not return."
        )

    parts = []
    if plan_name:
        parts.append(f"Your current plan is **{plan_name}**.")
    balance = f"You currently have **{_format_number(available)} credits available**"
    if reserved is not None:
        balance += f" and **{_format_number(reserved)} reserved**"
    parts.append(balance + ".")

    selected = _select_runway_estimates(message, context)
    wants_capacity = bool(_requested_capacity_kinds(message)) or any(
        cue in message.lower() for cue in ("how many", "can i create", "can i generate", "will i be able", "afford", "enough", "runway")
    )
    if not wants_capacity:
        return " ".join(parts)

    if not selected:
        parts.append(
            "I also checked the account-wide pricing runway, but no safe Face/Audio/Video runway estimate is currently available, so I won't invent a generation count."
        )
        return " ".join(parts)

    requested_count = _requested_count(message)
    run_based_results: list[bool] = []
    variable_metered = False
    for item in selected:
        line, count_result = _runway_line(item, requested_count)
        parts.append(line)
        if count_result is not None:
            run_based_results.append(count_result)
        if _normalized(item.get("unit")) != "runs":
            variable_metered = True

    if requested_count is not None:
        if run_based_results:
            if all(run_based_results):
                parts.append(f"For the run-based items above, **{requested_count} generations are within the current credit runway**.")
            elif not any(run_based_results):
                parts.append(f"For the run-based items above, **{requested_count} generations exceed the current credit runway**.")
        if variable_metered:
            parts.append(
                "For Audio and Video modes priced by characters, seconds or minutes, the exact number of complete items depends on script length and video duration. "
                "The figures above are the authoritative current runway units; once those lengths are known, the exact affordability calculation is deterministic."
            )

    return " ".join(parts)


def _nested_dict(value: object, *keys: str) -> dict:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def operational_account_answer(message: str, context: dict) -> str | None:
    """Answer common account-specific questions from the safe real-time envelope."""
    text = message.lower().strip()
    account = context.get("account") if isinstance(context.get("account"), dict) else {}

    if any(cue in text for cue in ("spend", "spent", "spending", "money paid", "paid this month", "usage this month")):
        spending = account.get("spending") if isinstance(account.get("spending"), dict) else {}
        credits = spending.get("credits") if isinstance(spending.get("credits"), dict) else {}
        money = spending.get("money") if isinstance(spending.get("money"), dict) else {}
        consumed = _as_number(credits.get("consumed"))
        paid = _as_number(money.get("paid"))
        currency = _normalized(money.get("currency")) or "USD"
        if consumed is None and paid is None:
            return "I checked your current account spending context, but the spending summary is unavailable right now."
        parts = ["For your current month:"]
        if consumed is not None:
            parts.append(f"you have used **{_format_number(consumed)} credits**")
        if paid is not None:
            symbol = "$" if currency.upper() == "USD" else f"{currency.upper()} "
            parts.append(f"and recorded payments total **{symbol}{_format_number(paid)}**")
        return " ".join(parts) + "."

    if any(cue in text for cue in ("saved work", "saved creations", "library", "saved face", "saved video", "saved audio")):
        library = account.get("library") if isinstance(account.get("library"), dict) else {}
        counts = library.get("counts") if isinstance(library.get("counts"), dict) else {}
        total = _as_number(library.get("total_visible"))
        if total is None:
            return "I checked your Saved work context, but the current library summary is unavailable right now."
        if not counts:
            return f"I can currently see **{_format_number(total)} saved items** in your authenticated Saved work view."
        ordered = [
            ("face", "Faces"), ("audio", "Audio"), ("video", "Videos"),
            ("group_photo", "Group Photos"), ("group_audio", "Group Audio"),
            ("group_video", "Group Videos"), ("multi_person_face", "Multi-Person Faces"),
            ("multi_person_audio", "Multi-Person Audio"), ("multi_person_video", "Multi-Person Videos"),
        ]
        details = [
            f"**{_format_number(counts.get(key))} {label}**"
            for key, label in ordered
            if _as_number(counts.get(key)) not in (None, 0)
        ]
        suffix = ", ".join(details[:6])
        return f"Your current Saved work view contains **{_format_number(total)} items**" + (f": {suffix}." if suffix else ".")

    if "notification" in text or "unread" in text:
        notifications = account.get("notifications") if isinstance(account.get("notifications"), dict) else {}
        unread = _as_number(notifications.get("unread_count"))
        category_counts = notifications.get("category_counts") if isinstance(notifications.get("category_counts"), dict) else {}
        if unread is None:
            return "I checked your account notifications, but the unread count is unavailable right now."
        active_categories = [
            f"{_format_number(value)} {key}"
            for key, value in category_counts.items()
            if (_as_number(value) or 0) > 0
        ]
        answer = f"You currently have **{_format_number(unread)} unread notifications**."
        if active_categories:
            answer += " Recent notification categories include " + ", ".join(active_categories[:4]) + "."
        return answer

    if "story" in text and any(cue in text for cue in ("recent", "review", "attention", "need", "status")):
        stories = account.get("stories") if isinstance(account.get("stories"), dict) else {}
        count = _as_number(stories.get("count"))
        recent = [item for item in list(stories.get("recent") or ()) if isinstance(item, dict)]
        if count is None:
            return "I checked your recent Multi-Person stories, but that context is unavailable right now."
        if not recent:
            return "I do not see any recent Multi-Person stories in your current authenticated account context."
        needs_review = [
            item for item in recent
            if _normalized(item.get("attention_state")).lower() == "awaiting_review"
        ]
        failed = [
            item for item in recent
            if _normalized(item.get("attention_state")).lower() == "failed"
        ]
        answer = f"I can see **{_format_number(count)} recent Multi-Person stories**."
        if needs_review:
            answer += f" **{len(needs_review)} need review**."
        if failed:
            answer += f" **{len(failed)} currently show a failed attention state**."
        latest = recent[0]
        stage = _normalized(latest.get("current_stage"))
        state = _normalized(latest.get("attention_state")) or _normalized(latest.get("workflow_state")) or _normalized(latest.get("state"))
        if state:
            answer += f" Your most recent story is **{state}**"
            answer += f" at **{stage}**." if stage else "."
        return answer

    if "plan" in text or "subscription" in text:
        pricing = context.get("pricing") if isinstance(context.get("pricing"), dict) else {}
        plan = pricing.get("plan") if isinstance(pricing.get("plan"), dict) else {}
        billing = account.get("billing") if isinstance(account.get("billing"), dict) else {}
        current_sub = billing.get("current_subscription") if isinstance(billing.get("current_subscription"), dict) else {}
        plan_name = (
            _normalized(plan.get("plan_name"))
            or _normalized(plan.get("plan_code"))
            or _normalized(current_sub.get("plan_name"))
            or _normalized(current_sub.get("plan_code"))
        )
        state = _normalized(current_sub.get("subscription_state")) or _normalized(current_sub.get("status"))
        if not plan_name:
            return "I checked your authenticated billing context, but your current plan is unavailable right now."
        answer = f"Your current desifaces plan is **{plan_name}**."
        if state:
            answer += f" Subscription state: **{state}**."
        return answer

    return None


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
            href = _ACTION_HREFS.get(str(action))
            actions.append(
                AssistantAction(
                    type=str(action),
                    label=label,
                    requires_confirmation=False if href else True,
                    href=href,
                )
            )
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

    async def _store_exchange(
        self,
        *,
        auth: AssistantAuthContext,
        session_id,
        user_content: str,
        assistant_content: str,
    ) -> None:
        await self._sessions.append(
            account_id=auth.account_id,
            user_id=auth.user_id,
            session_id=session_id,
            role="user",
            content=user_content,
        )
        await self._sessions.append(
            account_id=auth.account_id,
            user_id=auth.user_id,
            session_id=session_id,
            role="assistant",
            content=assistant_content,
        )

    async def chat(self, body: AssistantChatIn, auth: AssistantAuthContext) -> AssistantChatOut:
        session_id = body.session_id or uuid4()
        message_id = uuid4()

        redacted = redact_sensitive_text(body.message)
        decision = classify_restricted_request(body.message)
        if decision.restricted:
            await self._store_exchange(
                auth=auth,
                session_id=session_id,
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

        deterministic_answer = (
            operational_account_answer(safe_message, context)
            or operational_credit_answer(safe_message, context)
            or operational_generation_answer(safe_message, context)
        )
        if deterministic_answer is not None:
            guarded_answer, output_blocked = guard_output(deterministic_answer)
            await self._store_exchange(
                auth=auth,
                session_id=session_id,
                user_content=safe_message,
                assistant_content=guarded_answer,
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
            message=safe_message,
            history=history,
            context=context,
            knowledge=knowledge,
        )
        guarded_answer, output_blocked = guard_output(answer)

        await self._store_exchange(
            auth=auth,
            session_id=session_id,
            user_content=safe_message,
            assistant_content=guarded_answer,
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
