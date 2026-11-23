"""DEV-only global provider draft routing for creator-reviewed localization.

An experimental provider-language match is NOT an approved database route.
Only the authenticated and DEV-VM-attested preview handler may call this.
No canonical writes, approvals, TTS, or production activation.
"""
from __future__ import annotations

import os
import time

import httpx

from app.services.dev_translation_provider_adapters import (
    ProviderTranslationError,
    translate_via_configured_provider,
)
from app.services.dev101_natural_speech import (
    NaturalSpeechRejected,
    naturalize_provider_translation,
)


class DevGlobalRouteUnavailable(RuntimeError):
    pass


_DISCOVERY_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
_CACHE_SECONDS = 3600


async def _provider_languages(
    client: httpx.AsyncClient, provider: str
) -> frozenset[str]:
    cached = _DISCOVERY_CACHE.get(provider)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]

    if provider == "azure_translator":
        response = await client.get(
            "https://api.cognitive.microsofttranslator.com/languages",
            params={"api-version": "3.0", "scope": "translation"},
        )
        response.raise_for_status()
        codes = response.json()["translation"].keys()
    elif provider == "google_cloud_translation":
        response = await client.get(
            "https://translation.googleapis.com/language/translate/v2/languages",
            params={
                "key": os.environ["GOOGLE_CLOUD_TRANSLATION_API_KEY"],
                "model": "nmt",
            },
        )
        response.raise_for_status()
        codes = [
            item["language"]
            for item in response.json()["data"]["languages"]
        ]
    else:
        raise DevGlobalRouteUnavailable("unsupported_provider")

    result = frozenset(str(code).strip().lower() for code in codes)
    if not result:
        raise DevGlobalRouteUnavailable("empty_provider_language_catalog")
    _DISCOVERY_CACHE[provider] = (time.monotonic(), result)
    return result


async def resolve_dev_global_candidate(
    pool, client: httpx.AsyncClient, *,
    source_locale: str, target_locale: str
) -> dict[str, str]:
    """Select an explicit provider-compatible candidate without DB approval."""
    if os.getenv("DF_RUNTIME_ENV", "").lower() not in {"dev", "development"}:
        raise DevGlobalRouteUnavailable("dev_environment_required")
    if os.getenv("DF_DEV_GLOBAL_TRANSLATION_ENABLED", "").lower() != "true":
        raise DevGlobalRouteUnavailable("dev_global_translation_disabled")
    if (
        not source_locale or not target_locale
        or source_locale.casefold() == target_locale.casefold()
    ):
        raise DevGlobalRouteUnavailable("invalid_language_direction")

    async with pool.acquire() as conn:
        records = await conn.fetch(
            """SELECT locale, translator_lang, is_enabled,
                      translate_supported, tts_supported, is_user_selectable
               FROM public.tts_locales
               WHERE locale=$1 OR locale=$2""",
            source_locale, target_locale,
        )

    by_locale = {row["locale"]: row for row in records}
    source = by_locale.get(source_locale)
    target = by_locale.get(target_locale)
    if not source or not target:
        raise DevGlobalRouteUnavailable("language_not_in_dev_catalog")
    if not (
        source["is_enabled"] and source["translate_supported"]
        and target["is_enabled"] and target["translate_supported"]
        and target["tts_supported"] and target["is_user_selectable"]
    ):
        raise DevGlobalRouteUnavailable("language_not_end_to_end_eligible")

    # The explicit, existing DB provider-language mapping is authoritative.
    # Never guess a base language or silently flatten a region/script.
    source_code = str(source["translator_lang"] or "").strip()
    target_code = str(target["translator_lang"] or "").strip()
    if not source_code or not target_code:
        raise DevGlobalRouteUnavailable("provider_locale_mapping_missing")
    if source_code.lower() == target_code.lower():
        raise DevGlobalRouteUnavailable("regional_rewrite_not_certified")

    providers = (
        (
            "azure_translator", "text-translation-v3",
            bool(os.getenv("AZURE_TRANSLATOR_KEY"))
            and bool(os.getenv("AZURE_TRANSLATOR_REGION")),
        ),
        (
            "google_cloud_translation", "nmt",
            bool(os.getenv("GOOGLE_CLOUD_TRANSLATION_API_KEY")),
        ),
    )
    for provider, model, configured in providers:
        if not configured:
            continue
        try:
            codes = await _provider_languages(client, provider)
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            continue
        if source_code.lower() in codes and target_code.lower() in codes:
            return {
                "provider_code": provider,
                "model_code": model,
                "provider_source_code": source_code,
                "provider_target_code": target_code,
            }

    raise DevGlobalRouteUnavailable("no_verified_provider_language_mapping")


async def translate_global_dev_draft(
    pool, *, text: str, source_locale: str, target_locale: str,
    speaker_gender: str, original_context: dict
) -> tuple[str, str, str]:
    """Generate a DEV experimental draft; creator must still approve it."""
    if not isinstance(text, str) or not 0 < len(text.strip()) <= 1000:
        raise DevGlobalRouteUnavailable("invalid_text_length")

    async with httpx.AsyncClient(timeout=35) as client:
        route = await resolve_dev_global_candidate(
            pool, client,
            source_locale=source_locale,
            target_locale=target_locale,
        )
        translated = await translate_via_configured_provider(
            client, route=route, text=text, speaker_gender=speaker_gender
        )

    # Preserve existing contextual/natural-speech validation before HITL.
    try:
        draft = await naturalize_provider_translation(
            original_text=text,
            provider_draft=translated,
            source_locale=source_locale,
            target_locale=target_locale,
            speaker_gender=speaker_gender,
            original_context=original_context,
        )
    except NaturalSpeechRejected as exc:
        raise DevGlobalRouteUnavailable(
            "dev_global_natural_speech_review_failed"
        ) from exc

    if not draft.strip():
        raise DevGlobalRouteUnavailable("empty_global_translation_draft")
    return draft, route["provider_code"], route["model_code"]
