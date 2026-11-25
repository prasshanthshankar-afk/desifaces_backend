"""DEV101 R1 source/target language decision for Director and Compiler.

Locale identity is retained for speech and persistence. Translation only occurs
when the language differs, with English as the currently enabled source language.
This module does not approve routes, authorize providers, or relax HITL checks.
"""
from __future__ import annotations


def _language(locale: str) -> str:
    value = str(locale or "").strip().replace("_", "-")
    return value.split("-", 1)[0].casefold() if value else ""


def requires_translation(source_locale: str, target_locale: str) -> bool:
    """R1 English source; language-agnostic target, region-neutral decision.

    A missing target means no localization was requested. Missing/automatic
    source remains subject to the existing explicit-source validation.
    """
    source = _language(source_locale)
    target = _language(target_locale)
    if target and source and source != "en":
        raise ValueError("r1_english_source_required")
    return bool(target and source and target != source)
