"""DEV candidate: intent-first scene localization, before dialogue HITL.

No TTS, billing, persistence, autonomous approval, or provider fallback.
The caller must save/show returned drafts in its existing HITL flow.
"""
from __future__ import annotations

import json
import os
from typing import Any

 

class SceneLocalizationError(ValueError):
    pass


def _response_text(data: dict[str, Any]) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    return "\n".join(
        part["text"]
        for item in data.get("output", [])
        if isinstance(item, dict)
        for part in item.get("content", [])
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def validate_scene_draft(original: list[dict[str, Any]], result: Any) -> list[dict[str, str]]:
    """Shape check only; the existing dialogue HITL owns meaning acceptance."""
    if not isinstance(result, dict) or not isinstance(result.get("turns"), list):
        raise SceneLocalizationError("invalid_localized_scene_shape")
    drafts = result["turns"]
    if len(drafts) != len(original):
        raise SceneLocalizationError("localized_turn_count_mismatch")
    normalized = []
    seen = set()
    for source, draft in zip(original, drafts):
        if not isinstance(draft, dict):
            raise SceneLocalizationError("invalid_localized_turn")
        turn_id = str(source.get("turn_id", "")).strip()
        speaker = str(source.get("speaker", "")).strip()
        if not turn_id or not speaker or turn_id in seen:
            raise SceneLocalizationError("invalid_source_scene")
        if str(draft.get("turn_id", "")).strip() != turn_id or str(draft.get("speaker", "")).strip() != speaker:
            raise SceneLocalizationError("localized_turn_identity_mismatch")
        text = draft.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SceneLocalizationError("empty_localized_dialogue")
        seen.add(turn_id)
        normalized.append({"turn_id": turn_id, "speaker": speaker, "text": text.strip()})
    return normalized


async def localize_scene_for_hitl(
    *, turns: list[dict[str, Any]], source_locale: str, target_locale: str,
    scene_context: str, audience: str = "", feedback: str = "",
    model: str | None = None, client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Return draft for human review. This function never approves a dialogue."""
    if os.getenv("DF_RUNTIME_ENV", "").strip().lower() not in {"dev", "development"}:
        raise SceneLocalizationError("dev_environment_required")
    if os.getenv("DF_SCENE_INTENT_LOCALIZATION_ENABLED", "0").strip() != "1":
        raise SceneLocalizationError("scene_localization_not_enabled")
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise SceneLocalizationError("openai_credential_missing")
    if not 1 <= len(turns) <= 50 or not all(
        isinstance(t, dict) and str(t.get("turn_id", "")).strip()
        and str(t.get("speaker", "")).strip() and str(t.get("text", "")).strip()
        for t in turns
    ):
        raise SceneLocalizationError("invalid_source_scene")
    if not source_locale or not target_locale:
        raise SceneLocalizationError("missing_language_pair")

    instructions = (
        "You localize dramatic and conversational dialogue using the COMPLETE scene. "
        "Preserve the communicative intent and semantic roles: who acts, who receives "
        "the action, what is promised, who is addressed, the emotion, and the context. "
        "An English idiom like 'I can't wait to serve customers' expresses the "
        "speaker's eagerness to serve customers; it is NOT a request for someone "
        "else to serve in the café. Never reverse actor/recipient relationships. "
        "Do not add facts, offers, quantities, promises or new speakers. "
        "Write natural speakable language for the specified locale and audience, "
        "preserving the original ordered turn IDs and speaker names. "
        "Return ONLY a JSON object with 'turns', an array of objects containing "
        "turn_id, speaker, text. This is a draft for the CREATOR to approve, "
        "edit or regenerate. You do not grade, reject or approve its meaning."
    )
    request_data = {
        "source_locale": source_locale, "target_locale": target_locale,
        "scene_context": scene_context[:4000], "audience": audience[:300],
        "revision_feedback": feedback[:1000], "turns": turns,
    }
    body = {
        "model": model or os.getenv("DF_SCENE_INTENT_LOCALIZATION_MODEL", "gpt-4.1-mini"),
        "instructions": instructions,
        "input": json.dumps(request_data, ensure_ascii=False),
        "max_output_tokens": 5000,
    }
    import httpx  # Live API dependency; offline HITL contract tests need no SDK.
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        response = await client.post(
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
        )
        response.raise_for_status()
        raw = _response_text(response.json()).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        draft = validate_scene_draft(turns, json.loads(raw))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise SceneLocalizationError("scene_localization_failed") from exc
    finally:
        if owns_client:
            await client.aclose()
    return {
        "status": "pending_hitl",
        "source_locale": source_locale,
        "target_locale": target_locale,
        "provider": "openai",
        "model": body["model"],
        "turns": draft,
    }
