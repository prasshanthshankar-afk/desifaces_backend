"""Pure DEV provider-first translation routing policy; no live call-site wiring.

This module is intentionally dependency-free and cannot activate itself.
Only capability entries obtained from verified DEV database routing metadata
may be supplied by the caller. TTS routing and billing are separate concerns.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable


class TranslationSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class TranslationCapability:
    provider: str
    model: str
    source_locale: str
    target_locale: str
    enabled: bool
    approved: bool
    priority: int = 100


@dataclass(frozen=True)
class TranslationDecision:
    provider: str
    model: str
    source_locale: str
    target_locale: str


def choose_dev_translation(
    *,
    dev_runtime: bool,
    feature_enabled: bool,
    authenticated_user_id: str,
    allowlisted_user_ids: frozenset[str],
    source_locale: str,
    target_locale: str,
    capabilities: Iterable[TranslationCapability],
) -> TranslationDecision:
    """Fail closed; callers must retain legacy route OUTSIDE the experiment.

    Never treat a TTS voice/model capability as proof of translation support.
    No automatic GPT fallback, financial changes, or generated media writes.
    """
    if not dev_runtime or not feature_enabled:
        raise TranslationSelectionError("dev_translation_feature_unavailable")
    if not authenticated_user_id or authenticated_user_id not in allowlisted_user_ids:
        raise TranslationSelectionError("user_not_allowlisted")
    if not source_locale or not target_locale or source_locale == target_locale:
        raise TranslationSelectionError("invalid_translation_direction")
    eligible = sorted(
        (
            c for c in capabilities
            if c.enabled and c.approved
            and c.source_locale == source_locale
            and c.target_locale == target_locale
            and c.provider and c.model
        ),
        key=lambda c: (c.priority, c.provider, c.model),
    )
    if not eligible:
        raise TranslationSelectionError("no_verified_translation_capability")
    chosen = eligible[0]
    return TranslationDecision(
        provider=chosen.provider, model=chosen.model,
        source_locale=chosen.source_locale, target_locale=chosen.target_locale,
    )
