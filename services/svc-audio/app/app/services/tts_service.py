from __future__ import annotations

import os
import re
import json
from typing import Any, Dict, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

import httpx
import asyncpg

from app.services.azure_tts_service import AzureTTSService



class TerminalTTSValidationError(RuntimeError):
    """Deterministic input/locale/voice validation failure. Do not retry automatically."""


class RetryableTTSProviderError(RuntimeError):
    """Transient provider/transport failure that may succeed on retry."""


_LOCALE_ALIASES = {
    "in": "hi-IN",
    "india": "hi-IN",
    "hindi": "hi-IN",
    "hindi-india": "hi-IN",
    "hi": "hi-IN",
    "hi-in": "hi-IN",
    "english-india": "en-IN",
    "en-in": "en-IN",
    "en": "en-US",
    "tamil": "ta-IN",
    "ta": "ta-IN",
    "ta-in": "ta-IN",
    "telugu": "te-IN",
    "te": "te-IN",
    "te-in": "te-IN",
    "kannada": "kn-IN",
    "kn": "kn-IN",
    "kn-in": "kn-IN",
    "malayalam": "ml-IN",
    "ml": "ml-IN",
    "ml-in": "ml-IN",
    "marathi": "mr-IN",
    "mr": "mr-IN",
    "mr-in": "mr-IN",
    "gujarati": "gu-IN",
    "gu": "gu-IN",
    "gu-in": "gu-IN",
    "punjabi": "pa-IN",
    "pa": "pa-IN",
    "pa-in": "pa-IN",
    "bengali": "bn-IN",
    "bn": "bn-IN",
    "bn-in": "bn-IN",
}

_TRANSLATION_TARGET_ALIASES = {
    "in": "hi",
    "india": "hi",
    "hindi": "hi",
    "hindi-india": "hi",
    "hi": "hi",
    "hi-in": "hi",
    "english-india": "en",
    "en-in": "en",
    "en": "en",
    "tamil": "ta",
    "ta": "ta",
    "ta-in": "ta",
    "telugu": "te",
    "te": "te",
    "te-in": "te",
    "kannada": "kn",
    "kn": "kn",
    "kn-in": "kn",
    "malayalam": "ml",
    "ml": "ml",
    "ml-in": "ml",
    "marathi": "mr",
    "mr": "mr",
    "mr-in": "mr",
    "gujarati": "gu",
    "gu": "gu",
    "gu-in": "gu",
    "punjabi": "pa",
    "pa": "pa",
    "pa-in": "pa",
    "bengali": "bn",
    "bn": "bn",
    "bn-in": "bn",
}


def _normalize_speech_locale(locale: str) -> str:
    raw = (locale or "").strip().replace("_", "-")
    if not raw:
        raise TerminalTTSValidationError("missing_target_locale")
    key = raw.lower()
    if key in _LOCALE_ALIASES:
        return _LOCALE_ALIASES[key]
    if re.fullmatch(r"[a-z]{2,3}-[A-Z]{2,3}", raw):
        return raw
    if re.fullmatch(r"[a-z]{2,3}-[a-z]{2,3}", raw):
        lang, region = raw.split("-", 1)
        return f"{lang.lower()}-{region.upper()}"
    if re.fullmatch(r"[a-z]{2,3}", key):
        if key in _LOCALE_ALIASES:
            return _LOCALE_ALIASES[key]
    return raw


def _normalize_translation_target(target_locale: str, *, input_language: str = "") -> str:
    raw = (target_locale or "").strip().replace("_", "-").lower()
    if not raw:
        fallback = (input_language or "").strip().lower()
        return fallback or "en"
    if raw in _TRANSLATION_TARGET_ALIASES:
        return _TRANSLATION_TARGET_ALIASES[raw]
    if "-" in raw:
        return raw.split("-", 1)[0]
    return raw


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


    async def _voice_exists(self, voice_name: str) -> bool:
        row = await self.pool.fetchrow(
            """
            SELECT 1
            FROM public.tts_voices
            WHERE provider='azure' AND voice_name=$1
            LIMIT 1
            """,
            voice_name,
        )
        return bool(row)

    async def resolve_default_voice(self, *, locale: str, requested_voice: Optional[str]) -> str:
        """
        DB schema:
          - tts_locales has NO default_voice column
          - tts_voices has is_default boolean

        Rules:
          1) If requested_voice provided and exists, use it.
          2) Else pick default for exact locale (enabled locale only).
          3) Else fallback to base language match (hi-IN -> any hi-*)
        """
        locale = _normalize_speech_locale(locale)
        req = (requested_voice or "").strip()
        if req and req.lower() != "auto":
            if await self._voice_exists(req):
                return req
            # If caller asked for something invalid, we *fallback* instead of failing hard.
            # (You can make this strict later if you prefer.)

        # Exact locale, only if locale enabled
        row = await self.pool.fetchrow(
            """
            SELECT v.voice_name
            FROM public.tts_voices v
            JOIN public.tts_locales l
              ON l.locale = v.locale
            WHERE v.provider='azure'
              AND v.locale=$1
              AND l.is_enabled=true
            ORDER BY
              v.is_default DESC,
              CASE
                WHEN v.voice_type ILIKE 'Neural' THEN 0
                WHEN v.voice_name ILIKE '%Neural%' THEN 1
                ELSE 2
              END,
              CASE WHEN v.gender ILIKE 'Female' THEN 0 ELSE 1 END,
              v.voice_name ASC
            LIMIT 1
            """,
            locale,
        )
        if row and row.get("voice_name"):
            return str(row["voice_name"])

        # Base-language fallback: hi-IN -> hi-%
        base = _base_lang(locale)
        if base:
            row2 = await self.pool.fetchrow(
                """
                SELECT v.voice_name
                FROM public.tts_voices v
                JOIN public.tts_locales l
                  ON l.locale = v.locale
                WHERE v.provider='azure'
                  AND v.locale ILIKE $1
                  AND l.is_enabled=true
                ORDER BY
                  v.is_default DESC,
                  CASE
                    WHEN v.voice_type ILIKE 'Neural' THEN 0
                    WHEN v.voice_name ILIKE '%Neural%' THEN 1
                    ELSE 2
                  END,
                  CASE WHEN v.gender ILIKE 'Female' THEN 0 ELSE 1 END,
                  v.voice_name ASC
                LIMIT 1
                """,
                f"{base}-%",
            )
            if row2 and row2.get("voice_name"):
                return str(row2["voice_name"])

        raise TerminalTTSValidationError(f"no_voice_for_locale:{locale}")

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
    ) -> Tuple[bytes, str, str, str, str, Dict[str, Any]]:
        """
        Returns:
          (audio_bytes, final_text, chosen_voice, content_type, ext, meta)
        """
        target_lang = _base_lang(target_locale)
        final_text = text

        in_lang = _base_lang(input_language)
        if translate and in_lang and target_lang and in_lang != target_lang:    
            # Only attempt translation if key exists; otherwise fail clearly.
            final_text = await self.translate_text(text=text, to_lang=target_lang) 

        chosen_voice = await self.resolve_default_voice(locale=target_locale, requested_voice=voice)
        fmt = _normalize_output_format(output_format)

        # Build SSML (try style first; if Azure rejects, retry without express-as)
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

        try:
            if fmt == "wav":
                audio_bytes = await self.tts.synthesize_wav(ssml=ssml)
                return audio_bytes, final_text, chosen_voice, "audio/wav", "wav", {"output_format": "wav"}

            if fmt == "mp3":
                audio_bytes = await self.tts.synthesize_mp3(ssml=ssml)
                return audio_bytes, final_text, chosen_voice, "audio/mpeg", "mp3", {"output_format": "mp3"}

            # azure:<format>
            azure_fmt = fmt.split("azure:", 1)[1]
            audio_bytes = await self.tts.synthesize(ssml=ssml, output_format=azure_fmt)

            low = azure_fmt.lower()
            if "mp3" in low:
                content_type, ext = "audio/mpeg", "mp3"
            elif "riff" in low or "pcm" in low or "wav" in low:
                content_type, ext = "audio/wav", "wav"
            else:
                content_type, ext = "application/octet-stream", "bin"

            return audio_bytes, final_text, chosen_voice, content_type, ext, {"output_format": azure_fmt}

        except RuntimeError as e:
            # Common case: voice does not support style/expression
            msg = str(e)
            # If Azure returned a 4xx because of SSML express-as, retry without it once.
            ssml2 = self.build_ssml(
                text=final_text,
                locale=target_locale,
                voice=chosen_voice,
                style=None,
                emotion=None,
                rate=rate,
                pitch=pitch,
                allow_express_as=False,
            )
            if ssml2 != ssml:
                if fmt == "wav":
                    audio_bytes = await self.tts.synthesize_wav(ssml=ssml2)
                    return audio_bytes, final_text, chosen_voice, "audio/wav", "wav", {"output_format": "wav", "style_retried": True}
                if fmt == "mp3":
                    audio_bytes = await self.tts.synthesize_mp3(ssml=ssml2)
                    return audio_bytes, final_text, chosen_voice, "audio/mpeg", "mp3", {"output_format": "mp3", "style_retried": True}
                if fmt.startswith("azure:"):
                    azure_fmt = fmt.split("azure:", 1)[1]
                    audio_bytes = await self.tts.synthesize(ssml=ssml2, output_format=azure_fmt)
                    low = azure_fmt.lower()
                    if "mp3" in low:
                        content_type, ext = "audio/mpeg", "mp3"
                    elif "riff" in low or "pcm" in low or "wav" in low:
                        content_type, ext = "audio/wav", "wav"
                    else:
                        content_type, ext = "application/octet-stream", "bin"
                    return audio_bytes, final_text, chosen_voice, content_type, ext, {"output_format": azure_fmt, "style_retried": True}

            # If retry wasn’t applicable, bubble original error
            raise RuntimeError(msg)