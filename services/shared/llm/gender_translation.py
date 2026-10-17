from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict

import httpx


@dataclass(frozen=True)
class GenderTranslationResult:
    text: str
    provider: str
    model: str


class GenderTranslationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def normalize_gender(value: Any) -> str:
    raw = str(value or "").strip().lower()

    if raw in {"female", "f", "woman", "girl"}:
        return "female"
    if raw in {"male", "m", "man", "boy"}:
        return "male"
    if raw in {"neutral", "nonbinary", "non-binary"}:
        return "neutral"

    return "unspecified"


def _extract_output_text(payload: Dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parts: list[str] = []

    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue

        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue

            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())

    return "\n".join(parts).strip()


def _quality_review_instructions() -> str:
    return (
        "You are an independent bilingual QA reviewer of SPOKEN dialogue. "
        "Compare original English and proposed target-language speech. "
        "Reject unnatural literal calques, incorrect conversational register, "
        "unsupported added facts, omissions, and speaker/meaning changes. "
        "Return JSON only with keys approved (boolean), reason (short string). "
        "When uncertain, reject for human review. Do not rewrite the text."
    )


async def _quality_review(
    *, client: httpx.AsyncClient, base_url: str, headers: dict,
    model: str, request_input: dict, translated: str,
) -> None:
    """Optional strict DEV gate. Never silently approve if review is unavailable."""
    payload = {
        "model": os.getenv("DF_AUDIO_TRANSLATION_REVIEW_MODEL", "").strip() or model,
        "instructions": _quality_review_instructions(),
        "input": json.dumps(
            {"source": request_input["text"], "translation": translated,
             "source_language": request_input["source_language"],
             "target_language": request_input["target_language"],
             "previous_turn": request_input.get("previous_turn", ""),
             "next_turn": request_input.get("next_turn", ""),
             "tone": request_input.get("tone", "")},
            ensure_ascii=False),
        "max_output_tokens": 200,
    }
    try:
        response = await client.post(f"{base_url}/responses", headers=headers, json=payload)
        response.raise_for_status()
        verdict = json.loads(_extract_output_text(response.json()))
        approved = verdict.get("approved") is True if isinstance(verdict, dict) else False
    except Exception as exc:
        raise GenderTranslationError(
            f"spoken_translation_quality_review_unavailable:{type(exc).__name__}",
            retryable=False,
        ) from exc
    if not approved:
        raise GenderTranslationError(
            "spoken_translation_quality_review_rejected",
            retryable=False,
        )


def _clean_translation(text: str) -> str:
    out = str(text or "").strip()

    if out.startswith("```"):
        out = re.sub(r"^```(?:text|json|markdown)?\s*", "", out, flags=re.IGNORECASE)
        out = re.sub(r"\s*```$", "", out)

    if len(out) >= 2 and out[0] == out[-1] and out[0] in {'"', "'"}:
        out = out[1:-1].strip()

    return out


async def translate_with_gender(
    *,
    text: str,
    source_language: str,
    target_language: str,
    speaker_gender: str,
    tone: str = "neutral",
    previous_turn: str = "",
    next_turn: str = "",
    speaker_relationship: str = "",
    scene_context: str = "",
    locale: str = "",
    emotion: str = "",
) -> GenderTranslationResult:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise GenderTranslationError(
            "OPENAI_API_KEY is not configured",
            retryable=False,
        )

    gender = normalize_gender(speaker_gender)
    if gender not in {"female", "male"}:
        raise GenderTranslationError(
            f"gender-aware translation requires male or female; received {gender}",
            retryable=False,
        )

    normalized_tone = str(tone or "neutral").strip().lower()
    if normalized_tone not in {"neutral", "formal", "informal"}:
        normalized_tone = "neutral"

    model = (
        os.getenv("DF_AUDIO_TRANSLATION_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or "gpt-4.1-mini"
    )

    base_url = (
        os.getenv("OPENAI_BASE_URL", "").strip()
        or "https://api.openai.com/v1"
    ).rstrip("/")

    instructions = """
You are an expert dialogue localizer for a contemporary spoken-language
conversation, not a literal translation engine. Think like a native-language
screenwriter rewriting the same utterance for a real person to speak.
Produce idiomatic, everyday dialogue in the target locale.

Translate the supplied text naturally into the requested target language.

Requirements:
- Preserve the original meaning.
- The person speaking has the supplied grammatical gender.
- In languages that express grammatical gender, apply that gender consistently
  to first-person verbs, participles, adjectives, pronouns, honorifics, and
  self-references.
- Do not introduce gender where the target language or sentence does not require it.
- Preserve brand names, domain names, URLs, handles, numbers, product names,
  acronyms, and proper nouns exactly where appropriate.
- Adapt the dialogue as an authentic utterance, not a word-for-word translation.
- Reject literal calques of English idioms and connective words: translate the
  speaker's intention, not the surface grammar. Avoid stiff, textbook or
  bureaucratic constructions when the relationship is informal.
- Keep commonly spoken loanwords natural when local speakers normally use them;
  do not substitute obscure dictionary equivalents for social media, content,
  venture, or marketing terminology.
- Before replying, silently back-translate your proposed line and check that
  nothing was added, omitted, or inverted. In particular do not insert claims
  about numbers of languages, platforms, audiences or product capabilities.
- Silently test whether a native speaker would say the line aloud to this
  conversation partner; rewrite awkward syntax before output.
- Use the scene and adjacent turns to resolve pronouns and continuity only.
  Never incorporate a fact appearing only in a neighboring turn.
- Optimize for short, speakable clauses and the original emotional register,
  without sacrificing essential meaning.
- Prefer everyday target-language syntax and idioms appropriate to the locale,
  relationship, emotional delivery and scene context.
- Use neighboring turns only as context; translate ONLY the current text.
- Keep factual content, character intent and names intact; never invent claims,
  drop key meaning, change who speaks, or add dialogue from other turns.
- Keep the result speakable at a natural pace; no forced excessive compression.
- Respect the tone and speaker relationship for politeness and pronoun choice.
- Keep the result natural for speech synthesis.
- Do not provide explanations, notes, labels, alternatives, transliterations,
  quotation marks, or markdown.
- Return only the translated text.
""".strip()

    request_input = {
        "text": str(text or "").strip(),
        "source_language": str(source_language or "").strip(),
        "target_language": str(target_language or "").strip(),
        "speaker_gender": gender,
        "tone": normalized_tone,
        "locale": str(locale or "").strip()[:80],
        "previous_turn": str(previous_turn or "").strip()[:1000],
        "next_turn": str(next_turn or "").strip()[:1000],
        "speaker_relationship": str(speaker_relationship or "").strip()[:160],
        "scene_context": str(scene_context or "").strip()[:1000],
        "emotion": str(emotion or "").strip()[:120],
    }

    body = {
        "model": model,
        "instructions": instructions,
        "input": json.dumps(request_input, ensure_ascii=False),
        "max_output_tokens": 500,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(45.0, connect=10.0)

    responses_body = body
    chat_body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": instructions,
            },
            {
                "role": "user",
                "content": json.dumps(request_input, ensure_ascii=False),
            },
        ],
    }

    attempts = (
        (
            "openai_responses",
            f"{base_url}/responses",
            responses_body,
            3,
        ),
        (
            "openai_chat_completions",
            f"{base_url}/chat/completions",
            chat_body,
            2,
        ),
    )

    last_error: GenderTranslationError | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for provider, url, request_body, max_attempts in attempts:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await client.post(
                        url,
                        headers=headers,
                        json=request_body,
                    )
                except httpx.HTTPError as exc:
                    last_error = GenderTranslationError(
                        (
                            f"{provider} transport error "
                            f"attempt={attempt}/{max_attempts}: {exc}"
                        ),
                        retryable=True,
                    )

                    if attempt < max_attempts:
                        await asyncio.sleep(0.75 * (2 ** (attempt - 1)))
                        continue

                    break

                request_id = (
                    response.headers.get("x-request-id")
                    or response.headers.get("request-id")
                    or ""
                )

                if 200 <= response.status_code < 300:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        last_error = GenderTranslationError(
                            (
                                f"{provider} returned invalid JSON "
                                f"request_id={request_id or 'unknown'}"
                            ),
                            retryable=True,
                        )

                        if attempt < max_attempts:
                            await asyncio.sleep(0.75 * (2 ** (attempt - 1)))
                            continue

                        break

                    if provider == "openai_responses":
                        raw_text = _extract_output_text(payload)
                    else:
                        choices = payload.get("choices") or []
                        message = (
                            choices[0].get("message")
                            if choices and isinstance(choices[0], dict)
                            else None
                        )
                        raw_text = (
                            message.get("content", "")
                            if isinstance(message, dict)
                            else ""
                        )

                    translated = _clean_translation(raw_text)

                    if translated:
                        if os.getenv("DF_AUDIO_NATURAL_SPEECH_QA_REQUIRED", "0").strip().lower() in {"1", "true", "yes"}:
                            await _quality_review(
                                client=client, base_url=base_url, headers=headers,
                                model=model, request_input=request_input,
                                translated=translated,
                            )
                        return GenderTranslationResult(
                            text=translated,
                            provider=provider,
                            model=model,
                        )

                    last_error = GenderTranslationError(
                        (
                            f"{provider} returned empty text "
                            f"request_id={request_id or 'unknown'}"
                        ),
                        retryable=True,
                    )

                    if attempt < max_attempts:
                        await asyncio.sleep(0.75 * (2 ** (attempt - 1)))
                        continue

                    break

                body_excerpt = response.text[:500]
                retryable = (
                    response.status_code == 408
                    or response.status_code == 409
                    or response.status_code == 429
                    or response.status_code >= 500
                )

                last_error = GenderTranslationError(
                    (
                        f"{provider} failed "
                        f"status={response.status_code} "
                        f"attempt={attempt}/{max_attempts} "
                        f"request_id={request_id or 'unknown'} "
                        f"body={body_excerpt}"
                    ),
                    retryable=retryable,
                )

                if retryable and attempt < max_attempts:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay_seconds = float(retry_after) if retry_after else 0.0
                    except (TypeError, ValueError):
                        delay_seconds = 0.0

                    if delay_seconds <= 0:
                        delay_seconds = 0.75 * (2 ** (attempt - 1))

                    await asyncio.sleep(min(delay_seconds, 8.0))
                    continue

                if retryable:
                    # Exhausted this endpoint. Continue to the fallback endpoint.
                    break

                # A deterministic 4xx error should not be hidden by fallback.
                raise last_error

    raise last_error or GenderTranslationError(
        "gender translation failed without a provider response",
        retryable=True,
    )
