"""DEV-only provider translation. Does not change synthesis, wallets or billing."""
from __future__ import annotations
import json
import os
import socket
import uuid
import httpx


class DevTranslationRejected(RuntimeError):
    pass


def selected_for_dev(user_id: str | None) -> bool:
    if os.getenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "").lower() != "true":
        return False
    if os.getenv("DF_RUNTIME_ENV", "").lower() not in {"dev", "development"}:
        raise DevTranslationRejected("dev_environment_required")
    if socket.gethostname().split(".")[0] != "desifaces-dev" and os.getenv(
        "DF_DEV_TRANSLATION_HOST_ATTESTED", ""
    ).lower() != "true":
        raise DevTranslationRejected("dev_host_attestation_required")
    try:
        identity = str(uuid.UUID(str(user_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DevTranslationRejected("invalid_authenticated_user_id") from exc
    allowed = {
        v.strip().lower() for v in os.getenv("DF_DEV_TRANSLATION_USER_IDS", "").split(",")
        if v.strip()
    }
    return identity.lower() in allowed


async def translate_approved(
    pool, *, text: str, source_locale: str, target_locale: str,
    speaker_gender: str, original_context: dict,
) -> tuple[str, str, str]:
    """Approved directional DB row required. No automatic provider fallback."""
    # Exact approved locale direction first; base languages may resolve only when
    # they identify ONE approved canonical route. Never arbitrarily map en to en-IN.
    rows = await pool.fetch(
        """SELECT c.provider_code, c.model_code, c.provider_source_code,
                  c.provider_target_code, c.source_locale, c.target_locale
           FROM public.audio_translation_capabilities c
           JOIN public.tts_locales src ON src.locale=c.source_locale
           WHERE c.target_locale=$2
             AND (c.source_locale=$1 OR (
                strpos($1, '-')=0 AND lower(src.language_code)=lower($1)
             ))
             AND src.is_enabled AND c.is_enabled AND c.is_approved
           ORDER BY CASE WHEN c.source_locale=$1 THEN 0 ELSE 1 END,
                    c.priority,c.provider_code,c.model_code""",
        source_locale, target_locale,
    )
    if not rows:
        raise DevTranslationRejected("no_approved_translation_direction")
    exact = [r for r in rows if r["source_locale"] == source_locale]
    if exact:
        row = exact[0]
    else:
        canonical = {r["source_locale"] for r in rows}
        if len(canonical) != 1:
            raise DevTranslationRejected("ambiguous_source_locale")
        row = rows[0]
    provider, model = row["provider_code"], row["model_code"]
    if provider != "sarvam" or model != "mayura:v1":
        raise DevTranslationRejected("translation_provider_not_implemented")
    key = os.getenv("SARVAM_API_KEY", "").strip()
    if not key:
        raise DevTranslationRejected("sarvam_credential_missing")
    if not text.strip() or len(text) > 1000:
        raise DevTranslationRejected("invalid_text_length")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            result = await client.post(
                "https://api.sarvam.ai/translate",
                headers={"api-subscription-key": key},
                json={
                    "input": text,
                    "source_language_code": row["provider_source_code"],
                    "target_language_code": row["provider_target_code"],
                    "speaker_gender": speaker_gender.capitalize(),
                    "mode": "modern-colloquial",
                    "model": model,
                    "output_script": "spoken-form-in-native",
                    "numerals_format": "international",
                },
            )
            result.raise_for_status()
            candidate = result.json().get("translated_text", "")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise DevTranslationRejected("sarvam_translation_failed") from exc
        if not isinstance(candidate, str) or not candidate.strip():
            raise DevTranslationRejected("empty_translation")
        # Independent review is compulsory for DEV provider-first route.
        review_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not review_key:
            raise DevTranslationRejected("independent_qa_credential_missing")
        instructions = (
            "Independently review the translated spoken line against the original. "
            "Check conversational naturalness, correct contextual meaning, pronouns, "
            "grammar, speaker identity, proper names, no extra or missing claims. "
            "Do not add neighboring lines. Return JSON only: "
            '{"approved":true/false,"reason":"brief reason"}. '
            "If uncertain, reject."
        )
        try:
            qa = await client.post(
                (os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
                 + "/responses"),
                headers={"Authorization": "Bearer " + review_key},
                json={
                    "model": os.getenv("DF_DEV_TRANSLATION_QA_MODEL", "gpt-4.1-mini"),
                    "instructions": instructions,
                    "input": json.dumps({
                        "source": text, "candidate": candidate,
                        "source_locale": source_locale, "target_locale": target_locale,
                        "speaker_gender": speaker_gender,
                        "context": original_context,
                    }, ensure_ascii=False),
                    "max_output_tokens": 200,
                },
            )
            qa.raise_for_status()
            body = qa.json()
            verdict_text = body.get("output_text")
            if not verdict_text:
                verdict_text = "\n".join(
                    c.get("text", "")
                    for o in body.get("output", []) if isinstance(o, dict)
                    for c in o.get("content", []) if isinstance(c, dict)
                )
            verdict = json.loads(verdict_text)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise DevTranslationRejected("translation_qa_unavailable") from exc
        if not isinstance(verdict, dict) or verdict.get("approved") is not True:
            raise DevTranslationRejected("translation_quality_rejected")
    return candidate.strip(), provider, model
