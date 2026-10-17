"""Isolated DEV Sarvam Mayura translator. Not wired into billing or live routing.

Caller must enforce allowlist, live provider eligibility, and independent
bilingual review before synthesis. No automatic fallback on provider failure.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


class MayuraTranslationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MayuraTranslation:
    text: str
    provider: str = "sarvam"
    model: str = "mayura:v1"


# Mayura API's published coverage; do not infer TTS coverage from this set.
SUPPORTED_LOCALES = frozenset({
    "en-IN", "hi-IN", "bn-IN", "ta-IN", "te-IN", "gu-IN",
    "kn-IN", "ml-IN", "mr-IN", "pa-IN", "od-IN",
})


async def translate_mayura_dev(
    *,
    text: str,
    source_locale: str,
    target_locale: str,
    speaker_gender: str,
    client: httpx.AsyncClient | None = None,
) -> MayuraTranslation:
    """Translate exactly one line. Never sends neighboring turns as spoken text.

    Caller must run contextual naturalness QA and not use this output directly
    as final approved audio text without further certification.
    """
    if os.getenv("DF_DEV_SARVAM_PROVIDER_TRANSLATION_ENABLED", "false").lower() != "true":
        raise MayuraTranslationError("provider_translation_flag_off")
    if os.getenv("DF_RUNTIME_ENV", "").lower() not in {"dev", "development"}:
        raise MayuraTranslationError("dev_environment_required")
    if source_locale not in SUPPORTED_LOCALES or target_locale not in SUPPORTED_LOCALES:
        raise MayuraTranslationError("unsupported_translation_locale")
    if source_locale == target_locale:
        raise MayuraTranslationError("translation_not_required")
    # Mayura's documented translation direction is English <-> Indian language.
    if "en-IN" not in {source_locale, target_locale}:
        raise MayuraTranslationError("unsupported_mayura_translation_direction")
    if speaker_gender not in {"male", "female"}:
        raise MayuraTranslationError("unsupported_speaker_gender")
    if not text or len(text) > 1000:
        raise MayuraTranslationError("invalid_source_length")
    key = os.getenv("SARVAM_API_KEY", "").strip()
    if not key:
        raise MayuraTranslationError("sarvam_credential_missing")
    payload = {
        "input": text,
        "source_language_code": source_locale,
        "target_language_code": target_locale,
        "speaker_gender": speaker_gender.capitalize(),
        "mode": "modern-colloquial",
        "model": "mayura:v1",
        "output_script": "spoken-form-in-native",
        "numerals_format": "international",
    }
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0))
    try:
        response = await client.post(
            "https://api.sarvam.ai/translate",
            headers={"api-subscription-key": key, "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code != 200:
            raise MayuraTranslationError(f"mayura_http_{response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise MayuraTranslationError("mayura_invalid_json") from exc
        translated = result.get("translated_text") if isinstance(result, dict) else None
        if not isinstance(translated, str) or not translated.strip():
            raise MayuraTranslationError("mayura_empty_translation")
        return MayuraTranslation(text=translated.strip())
    except httpx.HTTPError as exc:
        raise MayuraTranslationError("mayura_transport_error") from exc
    finally:
        if owns_client:
            await client.aclose()
