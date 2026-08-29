from __future__ import annotations

from typing import Any

import httpx

from .config import settings
from .privacy import redact_sensitive_text
from .schemas import AssistantContextLocator

# V1 deliberately minimizes what downstream LLM processing can see. IDs,
# customer-authored prose, media references and identity/payment fields stay out
# of model context. The Assistant receives workflow facts, not raw customer data.
_SENSITIVE_KEY_FRAGMENTS = (
    "account_id", "project_id", "story_id", "scene_id", "participant_id", "turn_id",
    "media_id", "url", "uri", "token", "secret", "password", "email", "phone",
    "address", "card", "payment_method", "customer_id", "receipt", "provider_request",
    "dob", "birth", "passport", "license", "ssn", "government_id",
)
_ALLOWED_GENERATION_KEY_FRAGMENTS = (
    "state", "status", "stage", "retry", "error_code", "failure_code", "locale",
    "gender", "voice", "credits", "cost", "amount", "currency", "duration", "progress",
)
_ALLOWED_PRICING_KEY_FRAGMENTS = (
    "plan", "tier", "credits", "cost", "amount", "currency", "price", "unit", "quantity",
    "premium", "discount", "balance", "available", "required", "afford",
)


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_sensitive_text(value).text[:1000]
    return str(value)[:500]


def _safe_allowlisted_dict(value: Any, *, allowed_fragments: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 3:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            low = key_text.lower()
            if any(fragment in low for fragment in _SENSITIVE_KEY_FRAGMENTS):
                continue
            if not any(fragment in low for fragment in allowed_fragments):
                continue
            result[key_text[:100]] = _safe_allowlisted_dict(
                item,
                allowed_fragments=allowed_fragments,
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _safe_allowlisted_dict(item, allowed_fragments=allowed_fragments, depth=depth + 1)
            for item in value[:30]
        ]
    return _safe_scalar(value)


def _safe_generation(items: list[dict] | tuple[dict, ...] | None) -> list[dict]:
    safe: list[dict] = []
    for item in list(items or ())[:50]:
        if not isinstance(item, dict):
            continue
        projected = _safe_allowlisted_dict(
            item,
            allowed_fragments=_ALLOWED_GENERATION_KEY_FRAGMENTS,
        )
        if projected:
            safe.append(projected)
    return safe


def _safe_pricing(value: Any) -> dict[str, Any]:
    projected = _safe_allowlisted_dict(value or {}, allowed_fragments=_ALLOWED_PRICING_KEY_FRAGMENTS)
    return projected if isinstance(projected, dict) else {}


def project_safe_story_context(raw: dict[str, Any], locator: AssistantContextLocator) -> dict[str, Any]:
    participant_aliases: dict[str, str] = {}
    participants = []
    for index, item in enumerate(list(raw.get("participant_context") or ())[:20], start=1):
        if not isinstance(item, dict):
            continue
        pid = str(item.get("participant_id") or "")
        alias = f"Participant {index}"
        if pid:
            participant_aliases[pid] = alias
        participants.append({
            "alias": alias,
            "kind": _safe_scalar(item.get("kind")),
            "locale": _safe_scalar(item.get("locale")),
        })

    scenes = []
    for item in list(raw.get("scene_context") or ())[:50]:
        if not isinstance(item, dict):
            continue
        scenes.append({
            "sequence": item.get("sequence"),
            "state": _safe_scalar(item.get("state")),
        })

    dialogue = []
    for item in list(raw.get("dialogue_context") or ())[:100]:
        if not isinstance(item, dict):
            continue
        speaker_id = str(item.get("speaker_participant_id") or "")
        raw_text = item.get("text")
        dialogue.append({
            "sequence": item.get("sequence"),
            "kind": _safe_scalar(item.get("kind")),
            "speaker": participant_aliases.get(
                speaker_id,
                "Narrator" if not speaker_id else "Participant",
            ),
            "locale": _safe_scalar(item.get("locale")),
            "emotion": _safe_scalar(item.get("emotion")),
            "has_text": bool(str(raw_text or "").strip()),
            "text_length": len(str(raw_text or "")) if raw_text is not None else 0,
        })

    return {
        "surface": locator.surface,
        "screen": locator.screen,
        "creation_type": _safe_scalar(raw.get("creation_type")),
        "context_scope": _safe_scalar(raw.get("context_scope")),
        "participants": participants,
        "scenes": scenes,
        "dialogue": dialogue,
        "generation": _safe_generation(raw.get("generation_context")),
        "pricing": _safe_pricing(raw.get("pricing_context")),
        "allowed_actions": [
            str(action)[:100]
            for action in list(raw.get("allowed_assistant_actions") or ())[:30]
        ],
    }


class ContextResolver:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def resolve(self, locator: AssistantContextLocator, *, token: str) -> dict[str, Any]:
        basic = {"surface": locator.surface, "screen": locator.screen}
        if locator.story_id is None:
            return basic

        url = f"{settings.DF_DIRECTOR_BASE_URL}/api/director/stories/{locator.story_id}/assistant-context"
        params = {}
        if locator.scene_id is not None:
            params["scene_id"] = str(locator.scene_id)
        if locator.participant_id is not None:
            params["participant_id"] = str(locator.participant_id)

        response = await self._client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 404:
            return {**basic, "context_status": "not_found"}
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict):
            return basic
        return project_safe_story_context(raw, locator)
