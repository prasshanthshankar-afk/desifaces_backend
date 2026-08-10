from __future__ import annotations

import os
import re
import json
from typing import Any, Dict, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

import httpx
import asyncpg

from app.services.azure_tts_service import AzureTTSService
from app.repos.locale_catalog_repo import LocaleCatalogRepository
from app.repos.locale_context_repo import LocaleContextRepository
from app.repos.tts_catalog_repo import TTSCatalogRepository
from app.services.locale_resolver import LocaleResolver
from app.services.locale_context_resolver import LocaleContextResolver
from app.services.tts_model_resolver import TTSModelResolver
from app.services.tts_voice_resolver import TTSVoiceResolver
from app.services.tts_resolution_planner import (
    TTSResolutionPlanError,
    TTSResolutionPlanRequest,
    TTSResolutionPlanner,
)
from app.services.tts_provider_executor import TTSProviderExecutor
from app.services.tts_provider_adapter import TTSProviderAdapterError
from gender_translation import (
    GenderTranslationError,
    normalize_gender,
    translate_with_gender,
)



class TerminalTTSValidationError(RuntimeError):
    """Deterministic input/locale/voice validation failure. Do not retry automatically."""


class RetryableTTSProviderError(RuntimeError):
    """Transient provider/transport failure that may succeed on retry."""


def _normalize_speech_locale(locale: str) -> str:
    """
    Syntax normalization only.

    Semantic locale aliases and language/geography resolution belong to
    DB-backed locale masterdata.
    """
    raw = str(locale or "").strip().replace("_", "-")
    if not raw:
        raise TerminalTTSValidationError("missing_target_locale")

    parts = raw.split("-")

    if len(parts) == 1:
        return parts[0].lower()

    language = parts[0].lower()
    remainder = list(parts[1:])

    if remainder:
        region = remainder[-1]
        if len(region) == 2 and region.isalpha():
            remainder[-1] = region.upper()
        elif len(region) == 3 and region.isdigit():
            remainder[-1] = region

    return "-".join([language, *remainder])


def _normalize_translation_target(
    target_locale: str,
    *,
    input_language: str = "",
) -> str:
    """
    Derive only the base language identifier from an already resolved locale.
    No semantic locale mapping is encoded in application source.
    """
    raw = str(target_locale or "").strip().replace("_", "-")

    if raw:
        return raw.split("-", 1)[0].lower()

    fallback = str(input_language or "").strip().replace("_", "-")
    return fallback.split("-", 1)[0].lower() if fallback else "en"

def _should_translate(*, translate: bool, input_language: str, target_lang: str) -> bool:
    if not translate:
        return False
    in_lang = _base_lang(input_language)
    tgt = (target_lang or "").strip().lower()
    if not in_lang or not tgt:
        return False
    return in_lang != tgt

def _base_lang(locale: str) -> str:
    return (locale or "").split("-")[0].lower().strip()


def _normalize_output_format(fmt: Optional[str]) -> str:
    """
    Accepts: 'mp3' | 'wav' | Azure output-format strings
    Returns one of: 'mp3' | 'wav' | 'azure:<format>'
    """
    s = (fmt or "").strip()
    if not s:
        return "mp3"
    low = s.lower()
    if low in ("mp3",):
        return "mp3"
    if low in ("wav", "wave", "pcm"):
        return "wav"
    return f"azure:{s}"


def _resolution_output_format(value: Optional[str]) -> str:
    """Normalize concrete codec names to provider-neutral format families."""
    raw = str(value or "").strip().lower()

    if not raw:
        return "mp3"

    if "mp3" in raw:
        return "mp3"

    if (
        "wav" in raw
        or "wave" in raw
        or "riff" in raw
        or "pcm" in raw
    ):
        return "wav"

    return raw



def _safe_ssml_text(text: str) -> str:
    """
    Escape XML entities. Keep it simple + safe.
    """
    # Collapse whitespace a bit so SSML doesn't get weird pauses
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return _xml_escape(t)


class TTSService:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.tts = AzureTTSService()

        locale_catalog = LocaleCatalogRepository(pool)
        locale_context = LocaleContextRepository(pool)
        tts_catalog = TTSCatalogRepository(pool)

        self.resolution_planner = TTSResolutionPlanner(
            locale_resolver=LocaleResolver(locale_catalog),
            context_resolver=LocaleContextResolver(locale_context),
            model_resolver=TTSModelResolver(tts_catalog),
            voice_resolver=TTSVoiceResolver(tts_catalog),
        )
        self.provider_executor = TTSProviderExecutor()

        self.translator_key = os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
        self.translator_region = os.getenv("AZURE_TRANSLATOR_REGION", "").strip()
        self.translator_endpoint = os.getenv(
            "AZURE_TRANSLATOR_ENDPOINT",
            "https://api.cognitive.microsofttranslator.com",
        ).strip()

    async def translate_text(self, *, text: str, to_lang: str) -> str:
        """
        Translator:
        - If you have a regional/multi-service key, AZURE_TRANSLATOR_REGION is required.
        - If you have a global translator key, region may be optional.
        """
        if not self.translator_key:
            raise TerminalTTSValidationError("missing_azure_translator_key")

        normalized_to_lang = _normalize_translation_target(to_lang)
        if not normalized_to_lang:
            raise TerminalTTSValidationError(f"invalid_target_language to_lang={to_lang!r}")

        url = f"{self.translator_endpoint.rstrip('/')}/translate"
        params = {"api-version": "3.0", "to": normalized_to_lang}
        headers = {
            "Ocp-Apim-Subscription-Key": self.translator_key,
            "Content-Type": "application/json",
        }
        if self.translator_region:
            headers["Ocp-Apim-Subscription-Region"] = self.translator_region

        body = [{"text": text}]

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                r = await client.post(url, params=params, headers=headers, json=body)
            except httpx.HTTPError as e:
                raise RetryableTTSProviderError(f"translator_transport_error: {e}") from e

            if r.status_code == 200:
                j = r.json()
                return j[0]["translations"][0]["text"]

            body_text = r.text[:500]
            if r.status_code == 400 and ('"code":400036' in body_text or "target language is not valid" in body_text.lower()):
                raise TerminalTTSValidationError(
                    f"invalid_target_language to_lang={normalized_to_lang} body={body_text}"
                )
            if 400 <= r.status_code < 500:
                raise TerminalTTSValidationError(f"translator_failed status={r.status_code} body={body_text}")
            raise RetryableTTSProviderError(f"translator_failed status={r.status_code} body={body_text}")





    def build_ssml(
        self,
        *,
        text: str,
        locale: str,
        voice: str,
        style: Optional[str],
        emotion: Optional[str],
        rate: float = 1.0,
        pitch: float = 0.0,
        allow_express_as: bool = True,
    ) -> str:
        safe_text = _safe_ssml_text(text)

        # Guardrails
        try:
            rate = float(rate)
        except Exception:
            rate = 1.0
        try:
            pitch = float(pitch)
        except Exception:
            pitch = 0.0

        # 1.0 -> 0%, 1.1 -> +10%
        rate_pct = int((rate - 1.0) * 100)
        pitch_pct = int(pitch * 100)

        prosody = f'<prosody rate="{rate_pct:+d}%" pitch="{pitch_pct:+d}%">{safe_text}</prosody>'
        express_style = (style or "").strip() or (emotion or "").strip() or None

        if allow_express_as and express_style:
            inner = f'<mstts:express-as style="{_xml_escape(express_style)}">{prosody}</mstts:express-as>'
        else:
            inner = prosody

        return f"""<speak version="1.0"
  xmlns="http://www.w3.org/2001/10/synthesis"
  xmlns:mstts="http://www.w3.org/2001/mstts"
  xml:lang="{_xml_escape(locale)}">
  <voice name="{_xml_escape(voice)}">
    {inner}
  </voice>
</speak>"""

    async def synthesize(
        self,
        *,
        text: str,
        input_language: str,
        target_locale: str,
        voice: Optional[str],
        style: Optional[str],
        emotion: Optional[str],
        rate: float,
        pitch: float,
        translate: bool = True,
        output_format: Optional[str] = "mp3",
        speaker_gender: Optional[str] = None,
        voice_gender: Optional[str] = None,
        voice_locale: Optional[str] = None,
        translation_tone: Optional[str] = "neutral",
    ) -> Tuple[bytes, str, str, str, str, Dict[str, Any]]:
        """Synthesize speech and return audio plus resolved translation metadata.

        The selected provider voice is resolved before translation. This lets the
        authoritative voice catalog gender control grammatical agreement when the
        caller does not explicitly provide a speaker gender.
        """
        target_lang = _base_lang(target_locale)
        final_text = text

        requested_speaker_gender = normalize_gender(
            speaker_gender
        )
        requested_voice_gender = normalize_gender(
            voice_gender
        )

        if requested_voice_gender in {"female", "male"}:
            planner_gender = requested_voice_gender
        elif requested_speaker_gender in {"female", "male"}:
            planner_gender = requested_speaker_gender
        else:
            planner_gender = None

        try:
            resolution_plan = (
                await self.resolution_planner.resolve(
                    TTSResolutionPlanRequest(
                        requested_locale=target_locale,
                        text_length=len(text or ""),
                        output_format=(
                            _resolution_output_format(
                                output_format
                            )
                        ),
                        requested_voice=voice,
                        requested_gender=planner_gender,
                        requires_style=bool(style),
                        requires_emotion=bool(emotion),
                    )
                )
            )
        except TTSResolutionPlanError as exc:
            message = str(exc or "")

            terminal_markers = (
                "missing_requested_locale",
                "unknown_locale:",
                "alias_target_locale_unavailable:",
                "alias_target_missing:",
                "no_locale_for_language:",
                "ambiguous_locale:",
                "no_eligible_tts_model:",
                "ambiguous_tts_model_candidates:",
                "requested_voice_not_eligible:",
                "requested_voice_not_eligible_for_any_model:",
                "no_eligible_tts_voice:",
                "duplicate_requested_voice_candidates:",
                "ambiguous_tts_voice_candidates:",
                "provider_resolution_mismatch",
                "model_resolution_mismatch",
                "voice_locale_resolution_mismatch",
            )

            if any(
                marker in message
                for marker in terminal_markers
            ):
                raise TerminalTTSValidationError(
                    f"tts_resolution_failed:{message}"
                ) from exc

            # Do not expose raw infrastructure/DB errors.
            raise RetryableTTSProviderError(
                "tts_resolution_temporarily_unavailable"
            ) from exc

        chosen_voice = resolution_plan.voice_name

        authoritative_voice_gender = normalize_gender(
            resolution_plan.voice_gender
        )

        authoritative_voice_locale = str(
            resolution_plan.voice_home_locale
            or resolution_plan.canonical_locale
            or target_locale
        ).strip()

        if (
            requested_voice_gender in {"female", "male"}
            and authoritative_voice_gender in {"female", "male"}
            and requested_voice_gender != authoritative_voice_gender
        ):
            raise TerminalTTSValidationError(
                "voice_gender_mismatch:"
                f"requested={requested_voice_gender}:"
                f"catalog={authoritative_voice_gender}:"
                f"voice={chosen_voice}"
            )

        if voice_locale:
            requested_voice_locale = _normalize_speech_locale(voice_locale)
            catalog_voice_locale = _normalize_speech_locale(
                authoritative_voice_locale
            )
            if requested_voice_locale != catalog_voice_locale:
                raise TerminalTTSValidationError(
                    "voice_locale_mismatch:"
                    f"requested={requested_voice_locale}:"
                    f"catalog={catalog_voice_locale}:"
                    f"voice={chosen_voice}"
                )

        if requested_speaker_gender in {"female", "male", "neutral"}:
            resolved_speaker_gender = requested_speaker_gender
        elif authoritative_voice_gender in {"female", "male", "neutral"}:
            resolved_speaker_gender = authoritative_voice_gender
        else:
            resolved_speaker_gender = "unspecified"

        if (
            resolved_speaker_gender in {"female", "male"}
            and authoritative_voice_gender in {"female", "male"}
            and resolved_speaker_gender != authoritative_voice_gender
        ):
            raise TerminalTTSValidationError(
                "speaker_voice_gender_mismatch:"
                f"speaker={resolved_speaker_gender}:"
                f"voice={authoritative_voice_gender}:"
                f"voice_name={chosen_voice}"
            )

        normalized_tone = str(translation_tone or "neutral").strip().lower()
        if normalized_tone not in {"neutral", "formal", "informal"}:
            normalized_tone = "neutral"

        translation_provider: Optional[str] = None
        translation_model: Optional[str] = None
        in_lang = _base_lang(input_language)

        if _should_translate(
            translate=translate,
            input_language=input_language,
            target_lang=target_lang,
        ):
            if resolved_speaker_gender in {"female", "male"}:
                try:
                    translated = await translate_with_gender(
                        text=text,
                        source_language=in_lang,
                        target_language=target_lang,
                        speaker_gender=resolved_speaker_gender,
                        tone=normalized_tone,
                    )
                except GenderTranslationError as exc:
                    if getattr(exc, "retryable", False):
                        raise RetryableTTSProviderError(
                            f"gender_translation_failed:{exc}"
                        ) from exc
                    raise TerminalTTSValidationError(
                        f"gender_translation_failed:{exc}"
                    ) from exc

                final_text = translated.text
                translation_provider = translated.provider
                translation_model = translated.model
            else:
                # Existing Azure Translator v3 path remains available for
                # neutral/unspecified requests and backward-compatible clients.
                final_text = await self.translate_text(
                    text=text,
                    to_lang=target_lang,
                )
                translation_provider = "azure_translator_v3"

        base_meta: Dict[str, Any] = {
            "speaker_gender": resolved_speaker_gender,
            "voice_gender": authoritative_voice_gender,
            "voice_locale": authoritative_voice_locale,
            "translation_tone": normalized_tone,
        }
        if final_text != text:
            base_meta["translated_text"] = final_text
        if translation_provider:
            base_meta["translation_provider"] = translation_provider
        if translation_model:
            base_meta["translation_model"] = translation_model

        def result_meta(output_value: str, *, style_retried: bool = False) -> Dict[str, Any]:
            meta = dict(base_meta)
            meta["output_format"] = output_value
            if style_retried:
                meta["style_retried"] = True
            return meta

        fmt = _normalize_output_format(output_format)

        # Provider-neutral execution. Provider/model/voice selection
        # has already been resolved from DB-backed masterdata.
        execution_format = _resolution_output_format(output_format)

        ssml = self.build_ssml(
            text=final_text,
            locale=target_locale,
            voice=chosen_voice,
            style=style,
            emotion=emotion,
            rate=rate,
            pitch=pitch,
            allow_express_as=True,
        )

        style_retried = False
        try:
            provider_result = await self.provider_executor.synthesize(
                plan=resolution_plan,
                text=final_text,
                output_format=execution_format,
                ssml=ssml,
                style=style,
                emotion=emotion,
                rate=rate,
                pitch=pitch,
            )
        except TTSProviderAdapterError as exc:
            if resolution_plan.adapter_key != "azure" or not (style or emotion):
                raise RetryableTTSProviderError(str(exc)) from exc

            retry_ssml = self.build_ssml(
                text=final_text,
                locale=target_locale,
                voice=chosen_voice,
                style=None,
                emotion=None,
                rate=rate,
                pitch=pitch,
                allow_express_as=False,
            )
            try:
                provider_result = await self.provider_executor.synthesize(
                    plan=resolution_plan,
                    text=final_text,
                    output_format=execution_format,
                    ssml=retry_ssml,
                    style=None,
                    emotion=None,
                    rate=rate,
                    pitch=pitch,
                )
                style_retried = True
            except TTSProviderAdapterError as retry_exc:
                raise RetryableTTSProviderError(str(retry_exc)) from retry_exc

        meta = result_meta(execution_format, style_retried=style_retried)
        meta.update(dict(provider_result.metadata or {}))
        meta["provider_code"] = provider_result.provider_code
        meta["model_code"] = provider_result.model_code

        return (
            provider_result.audio_bytes,
            final_text,
            provider_result.voice_name,
            provider_result.content_type,
            provider_result.extension,
            meta,
        )
