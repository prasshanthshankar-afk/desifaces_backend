# services/svc-face/app/app/services/translation_service.py
from __future__ import annotations
from typing import Tuple
from deep_translator import GoogleTranslator

class TranslationService:
    """Free translation service using Google Translator (no API key needed)"""
    
    SUPPORTED_LANGUAGES = {
        "en": "English",
        "hi": "Hindi",
        "ta": "Tamil",
        "te": "Telugu",
        "kn": "Kannada",
        "ml": "Malayalam",
        "bn": "Bengali",
        "mr": "Marathi",
        "gu": "Gujarati",
        "pa": "Punjabi"
    }
    
    async def translate_to_english(self, text: str, source_lang: str) -> Tuple[str, bool]:
        """
        Translate user input from regional language to English.
        Returns (translated_text, success)
        """
        if not text or not text.strip():
            return "", False
        
        # Already English
        if source_lang == "en":
            return text, True
        
        # Validate source language
        if source_lang not in self.SUPPORTED_LANGUAGES:
            return text, False  # Return original if unsupported
        
        try:
            translator = GoogleTranslator(source=source_lang, target='en')
            translated = translator.translate(text)
            
            if not translated or len(translated) < 3:
                return text, False
            
            return translated, True
        
        except Exception as e:
            # If translation fails, return original
            return text, False
    
    async def validate_translation(self, original: str, translated: str, source_lang: str) -> bool:
        """
        Back-translate to verify accuracy.
        Returns True if translation seems accurate.
        """
        if source_lang == "en":
            return True
        
        try:
            back_translator = GoogleTranslator(source='en', target=source_lang)
            back_translated = back_translator.translate(translated)
            
            # Simple similarity check (word overlap)
            original_words = set(original.lower().split())
            back_words = set(back_translated.lower().split())
            
            if not original_words or not back_words:
                return False
            
            overlap = len(original_words & back_words)
            total = len(original_words)
            
            similarity = overlap / total if total > 0 else 0
            
            # Accept if >60% word overlap
            return similarity > 0.6
        
        except Exception:
            return False
    
    def get_error_message(self, error_code: str, language: str) -> str:
        """Get error message in user's language"""
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
            }
        }
        
        return messages.get(error_code, {}).get(language, messages[error_code].get("en", "Error"))