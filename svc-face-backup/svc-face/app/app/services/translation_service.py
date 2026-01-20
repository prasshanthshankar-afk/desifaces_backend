from __future__ import annotations

import asyncio
import time
from typing import Tuple, Optional, Dict
from functools import lru_cache

from deep_translator import GoogleTranslator


class TranslationService:
    """Free translation service using deep_translator (GoogleTranslator)."""

    SUPPORTED_LANGUAGES: Dict[str, str] = {
        "en": "English",
        "hi": "Hindi",
        "ta": "Tamil",
        "te": "Telugu",
        "kn": "Kannada",
        "ml": "Malayalam",
        "bn": "Bengali",
        "mr": "Marathi",
        "gu": "Gujarati",
        "pa": "Punjabi",
    }

    # tiny cache to avoid repeated translations of same prompt
    # (useful because users often retry the same text)
    @staticmethod
    @lru_cache(maxsize=2048)
    def _cached_translate(source_lang: str, text: str) -> str:
        translator = GoogleTranslator(source=source_lang, target="en")
        return translator.translate(text)

    @staticmethod
    @lru_cache(maxsize=2048)
    def _cached_back_translate(target_lang: str, text_en: str) -> str:
        translator = GoogleTranslator(source="en", target=target_lang)
        return translator.translate(text_en)

    def _normalize_lang(self, lang: Optional[str]) -> str:
        if not lang:
            return "en"
        lang = lang.strip().lower()
        # handle common BCP-47 variants: hi-IN -> hi
        if "-" in lang:
            lang = lang.split("-")[0]
        return lang

    async def translate_to_english(self, text: str, source_lang: str) -> Tuple[str, bool]:
        """
        Translate user input from regional language to English.
        Returns (translated_text, success)
        """
        if not text or not text.strip():
            return "", False

        source_lang = self._normalize_lang(source_lang)

        # Already English
        if source_lang == "en":
            return text.strip(), True

        # Unsupported language: return original (do not fail hard)
        if source_lang not in self.SUPPORTED_LANGUAGES:
            return text.strip(), False

        cleaned = " ".join(text.strip().split())
        try:
            # Run blocking translation in a worker thread.
            translated = await asyncio.to_thread(self._cached_translate, source_lang, cleaned)

            translated = (translated or "").strip()
            # basic sanity checks
            if len(translated) < 3:
                return cleaned, False

            # If translation returns identical text, likely failed or already English/transliterated
            if translated.lower() == cleaned.lower():
                return cleaned, False

            return translated, True

        except Exception:
            return cleaned, False

    async def validate_translation(self, original: str, translated: str, source_lang: str) -> bool:
        """
        Back-translate to verify accuracy.
        Returns True if translation seems plausible.
        """
        source_lang = self._normalize_lang(source_lang)
        if source_lang == "en":
            return True
        if source_lang not in self.SUPPORTED_LANGUAGES:
            return False
        if not original or not translated:
            return False

        original = " ".join(original.strip().split())
        translated = " ".join(translated.strip().split())

        try:
            back = await asyncio.to_thread(self._cached_back_translate, source_lang, translated)
            back = (back or "").strip()
            if len(back) < 3:
                return False

            # Heuristic: character overlap (script-friendly) instead of word overlap
            o = set([c for c in original.lower() if c.isalnum()])
            b = set([c for c in back.lower() if c.isalnum()])
            if not o or not b:
                return False

            overlap = len(o & b)
            similarity = overlap / max(len(o), 1)

            # relaxed threshold because back-translation is noisy
            return similarity >= 0.25

        except Exception:
            return False

    def get_error_message(self, error_code: str, language: str) -> str:
        """Get error message in user's language (never throws)."""
        language = self._normalize_lang(language)

        messages = {
            "unsafe_prompt": {
                "en": "Your request contains inappropriate content. Please try again.",
                "hi": "आपके अनुरोध में अनुचित सामग्री है। कृपया पुन: प्रयास करें।",
                "ta": "உங்கள் கோரிக்கையில் பொருத்தமற்ற உள்ளடக்கம் உள்ளது. மீண்டும் முயற்சிக்கவும்.",
                "te": "మీ అభ్యర్థనలో అనుచితమైన కంటెంట్ ఉంది. దయచేసి మళ్లీ ప్రయత్నించండి.",
            },
            "translation_failed": {
                "en": "Could not understand your request. Please rephrase.",
                "hi": "आपके अनुरोध को समझ नहीं पाए। कृपया दोबारा लिखें।",
                "ta": "உங்கள் கோரிக்கையைப் புரிந்து கொள்ள முடியவில்லை. மீண்டும் எழுதவும்.",
                "te": "మీ అభ్యర్థనను అర్థం చేసుకోలేకపోయాను. దయచేసి మళ్లీ వ్రాయండి.",
            },
            "generation_failed": {
                "en": "Image generation failed. Please try again.",
                "hi": "छवि निर्माण विफल। कृपया पुन: प्रयास करें।",
                "ta": "படத்தை உருவாக்க முடியவில்லை. மீண்டும் முயற்சிக்கவும்.",
                "te": "చిత్రం సృష్టి విఫలమైంది. దయచేసి మళ్లీ ప్రయత్నించండి.",
            },
        }

        bucket = messages.get(error_code) or {}
        return bucket.get(language) or bucket.get("en") or "Error"