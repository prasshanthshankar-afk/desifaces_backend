"""Foundation-only source/translation separation for DEV Hindi HITL.

Do not infer language from an output locale. Refuse clearly non-English
source text instead of mislabeling it as canonical English.
"""
from __future__ import annotations
import unicodedata

def validate_english_source(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("canonical_english_source_missing")
    # Indic-script text in an en-US source is a definite mismatch for this
    # English-to-Hindi route; do not pretend it is the English original.
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if not letters:
        raise ValueError("canonical_english_source_missing")
    devanagari = sum("\u0900" <= c <= "\u097f" for c in letters)
    if devanagari and devanagari * 2 >= len(letters):
        raise ValueError("canonical_english_source_locale_mismatch")
