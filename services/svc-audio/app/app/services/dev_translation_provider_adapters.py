"""Provider-neutral text translation execution for approved DEV database routes.

ElevenLabs remains in the independent TTS catalog; its dubbing capability is
not interchangeable with a synchronous text-translation API.
"""
from __future__ import annotations

import os
import httpx


class ProviderTranslationError(RuntimeError):
    pass


async def translate_via_configured_provider(
    client: httpx.AsyncClient, *, route: dict, text: str, speaker_gender: str,
) -> str:
    """No guessed provider or locale, no unapproved fallback."""
    provider = route["provider_code"]
    model = route["model_code"]
    source = route["provider_source_code"]
    target = route["provider_target_code"]
    if not text.strip() or len(text) > 1000:
        raise ProviderTranslationError("invalid_text_length")
    if provider == "sarvam":
        if model != "mayura:v1":
            raise ProviderTranslationError("unsupported_sarvam_model")
        key = os.getenv("SARVAM_API_KEY", "").strip()
        if not key:
            raise ProviderTranslationError("sarvam_credential_missing")
        response = await client.post(
            "https://api.sarvam.ai/translate",
            headers={"api-subscription-key": key},
            json={
                "input": text, "source_language_code": source,
                "target_language_code": target,
                "speaker_gender": speaker_gender.capitalize(),
                "mode": "modern-colloquial", "model": model,
                "output_script": "spoken-form-in-native",
                "numerals_format": "international",
            },
        )
        response.raise_for_status()
        result = response.json().get("translated_text")
    elif provider in {"azure", "azure_translator"}:
        # Microsoft Translator's REST request uses language codes such as es,
        # ar and fr; provider codes are taken exclusively from approved metadata.
        if model not in {"text-translation-v3", "azure-translator-v3"}:
            raise ProviderTranslationError("unsupported_azure_model")
        key = os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
        region = os.getenv("AZURE_TRANSLATOR_REGION", "").strip()
        if not key or not region:
            raise ProviderTranslationError("azure_translator_credential_missing")
        response = await client.post(
            "https://api.cognitive.microsofttranslator.com/translate",
            params={"api-version": "3.0", "from": source, "to": target},
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "Ocp-Apim-Subscription-Region": region,
                "Content-Type": "application/json",
            },
            json=[{"Text": text}],
        )
        response.raise_for_status()
        body = response.json()
        items = body[0].get("translations", []) if isinstance(body, list) and len(body) == 1 else []
        matches = [item.get("text") for item in items if isinstance(item, dict)
                   and str(item.get("to", "")).lower() == target.lower()]
        result = matches[0] if len(matches) == 1 else None
    elif provider == "google_cloud_translation":
        # Optional Google Cloud Translation Basic NMT; no implicit activation.
        if model != "nmt":
            raise ProviderTranslationError("unsupported_google_model")
        key = os.getenv("GOOGLE_CLOUD_TRANSLATION_API_KEY", "").strip()
        if not key:
            raise ProviderTranslationError("google_translation_credential_missing")
        response = await client.post(
            "https://translation.googleapis.com/language/translate/v2",
            params={"key": key},
            json={"q": text, "source": source, "target": target,
                  "format": "text", "model": "nmt"},
        )
        response.raise_for_status()
        body = response.json()
        translations = body.get("data", {}).get("translations", [])
        result = (translations[0].get("translatedText")
                  if isinstance(translations, list) and len(translations) == 1
                  and isinstance(translations[0], dict) else None)
    else:
        raise ProviderTranslationError("translation_provider_not_implemented")
    if not isinstance(result, str) or not result.strip():
        raise ProviderTranslationError("empty_provider_translation")
    return result.strip()
