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
    # Existing-schema translation contract: stored only in target locale
    # meta_json.translation_routing_v1. No new table and no implicit fallback.
    records = await pool.fetch(
        """SELECT locale, language_code, meta_json
           FROM public.tts_locales
           WHERE is_enabled AND (
             locale=$2 OR locale=$1 OR
             (strpos($1,'-')=0 AND lower(language_code)=lower($1))
           )""",
        source_locale, target_locale,
    )
    source_locales = {
        r["locale"] for r in records
        if r["locale"] == source_locale or (
            "-" not in source_locale and
            str(r["language_code"] or "").lower() == source_locale.lower()
        )
    }
    targets = [r for r in records if r["locale"] == target_locale]
    if len(targets) != 1 or not source_locales:
        raise DevTranslationRejected("no_approved_translation_direction")
    metadata = targets[0]["meta_json"] or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError as exc:
            raise DevTranslationRejected("invalid_translation_metadata") from exc
    routing = metadata.get("translation_routing_v1") if isinstance(metadata, dict) else None
    if not isinstance(routing, dict) or routing.get("enabled") is not True:
        raise DevTranslationRejected("no_approved_translation_direction")
    entries = routing.get("capabilities")
    if not isinstance(entries, list):
        raise DevTranslationRejected("invalid_translation_metadata")
    candidates = []
    for c in entries:
        if not isinstance(c, dict) or c.get("approved") is not True or c.get("enabled") is not True:
            continue
        if c.get("source_locale") not in source_locales or c.get("target_locale") != target_locale:
            continue
        if not all(isinstance(c.get(k), str) and c[k].strip() for k in (
            "provider", "model", "provider_source_code", "provider_target_code"
        )):
            continue
        candidates.append(c)
    if not candidates:
        raise DevTranslationRejected("no_approved_translation_direction")
    exact = [c for c in candidates if c["source_locale"] == source_locale]
    pool_candidates = exact or candidates
    if len({c["source_locale"] for c in pool_candidates}) != 1:
        raise DevTranslationRejected("ambiguous_source_locale")
    if len(pool_candidates) != 1:
        raise DevTranslationRejected("ambiguous_translation_route")
    c = pool_candidates[0]
    row = dict(provider_code=c["provider"], model_code=c["model"],
               provider_source_code=c["provider_source_code"],
               provider_target_code=c["provider_target_code"])
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
