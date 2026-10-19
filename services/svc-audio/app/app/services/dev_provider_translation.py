"""DEV-only provider translation. Does not change synthesis, wallets or billing."""
from __future__ import annotations
import json
import os
import re
import urllib.request
import uuid
import httpx


class DevTranslationRejected(RuntimeError):
    pass


def selected_for_dev(user_id: str | None) -> bool:
    if os.getenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "").lower() != "true":
        return False
    if os.getenv("DF_RUNTIME_ENV", "").lower() not in {"dev", "development"}:
        raise DevTranslationRejected("dev_environment_required")
    # Environment flags and Docker hostnames are not trusted host identity.
    # Azure Instance Metadata Service binds the decision to the actual VM.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01",
            headers={"Metadata": "true"},
        )
        with opener.open(request, timeout=2) as response:
            vm = json.load(response)
        trusted = (
            vm.get("name") == "desifaces-dev"
            and str(vm.get("resourceGroupName", "")).lower() == "rg-desifaces-dev"
            and str(vm.get("subscriptionId", "")).lower()
            == "e70463a7-4016-4cf5-9291-b640b0f0f5cb"
        )
    except Exception as exc:
        raise DevTranslationRejected("dev_host_attestation_required") from exc
    if not trusted:
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


async def _detect_auto_source_language(text: str) -> str:
    """Resolve UI 'auto' to an ISO 639-1 language via independent detection.

    Never infer geographic dialect, approve a provider, or default to English.
    Routing still requires a unique enabled source locale and approved DB edge.
    """
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise DevTranslationRejected("auto_source_detection_credential_missing")
    if not text.strip() or len(text) > 1000:
        raise DevTranslationRejected("invalid_text_length")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses",
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": os.getenv("DF_DEV_TRANSLATION_QA_MODEL", "gpt-4.1-mini"),
                    "instructions": (
                        "Identify the language of the CURRENT spoken utterance only, "
                        "not the requested output language or surrounding dialogue. "
                        "Return JSON ONLY with language_code, a lowercase ISO 639-1 "
                        "two-letter code, or null if uncertain, mixed or ambiguous. "
                        "Never assume English from context or invent a regional locale."
                    ),
                    "input": json.dumps({"utterance": text}, ensure_ascii=False),
                    "max_output_tokens": 100,
                },
            )
            response.raise_for_status()
            body = response.json()
            output = body.get("output_text") or "\\n".join(
                c.get("text", "") for o in body.get("output", []) if isinstance(o, dict)
                for c in o.get("content", []) if isinstance(c, dict)
            )
            detected = json.loads(output).get("language_code")
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise DevTranslationRejected("auto_source_detection_failed") from exc
    if not isinstance(detected, str) or not re.fullmatch(r"[a-z]{2}", detected):
        raise DevTranslationRejected("auto_source_language_ambiguous")
    return detected


async def translate_approved(
    pool, *, text: str, source_locale: str, target_locale: str,
    speaker_gender: str, original_context: dict,
) -> tuple[str, str, str]:
    """Approved directional DB row required. No automatic provider fallback."""
    if str(source_locale or "").strip().lower() == "auto":
        source_locale = await _detect_auto_source_language(text)
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
    if not text.strip() or len(text) > 1000:
        raise DevTranslationRejected("invalid_text_length")
    # Preserve early fail-closed credential validation before attempting imports
    # or external calls. Provider adapters also validate credentials.
    if provider == "sarvam" and not os.getenv("SARVAM_API_KEY", "").strip():
        raise DevTranslationRejected("sarvam_credential_missing")
    if provider in {"azure", "azure_translator"} and not (
        os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
        and os.getenv("AZURE_TRANSLATOR_REGION", "").strip()
    ):
        raise DevTranslationRejected("azure_translator_credential_missing")
    from app.services.dev_translation_provider_adapters import (
        translate_via_configured_provider,
    )
    # Provider-first DEV route: independent language review is advisory, never
    # a gate for TTS. Keep DB direction approval, provider validation and
    # upstream failure behavior unchanged. The canonical source dialogue
    # remains in Director; no mutation of the original turn.
    async with httpx.AsyncClient(timeout=30) as client:
        translated = await translate_via_configured_provider(
            client, route=row, text=text, speaker_gender=speaker_gender,
        )
    if not isinstance(translated, str) or not translated.strip():
        raise DevTranslationRejected("empty_provider_translation")
    return translated.strip(), provider, model
