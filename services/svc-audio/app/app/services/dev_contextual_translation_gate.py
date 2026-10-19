"""Fail-closed bounded contextual speech repair. No provider, TTS or billing coupling.

Meaning clarification is source-language only; provider translator still produces
target-language output. A separate reviewer checks against the original source.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Awaitable, Callable


class TranslationReviewRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class Review:
    approved: bool
    reason: str


@dataclass(frozen=True)
class ApprovedTranslation:
    text: str
    attempts: int
    meaning: str


async def contextual_translation_with_review(
    *,
    original: str,
    previous_turn: str,
    next_turn: str,
    clarify_meaning: Callable[[str, str, str], Awaitable[str]],
    provider_translate: Callable[[str], Awaitable[str]],
    independent_review: Callable[[str, str, str, str, str], Awaitable[Review]],
    max_repairs: int = 2,
    repair_target_speech: Callable[[str, str, str, str], Awaitable[str]] | None = None,
) -> ApprovedTranslation:
    if not original.strip() or max_repairs < 0 or max_repairs > 2:
        raise TranslationReviewRequired("invalid_translation_contract")
    try:
        meaning = (await clarify_meaning(original, previous_turn, next_turn)).strip()
    except Exception as exc:
        raise TranslationReviewRequired("meaning_clarification_failed") from exc
    if not meaning:
        raise TranslationReviewRequired("empty_meaning_clarification")

    # Always check candidate against original AND context; never certify against
    # clarified text alone. One initial attempt + at most two repairs.
    repaired_candidate = None
    last_rejection_category = "unknown"
    for attempt in range(max_repairs + 1):
        try:
            candidate = (repaired_candidate if repaired_candidate is not None
                         else await provider_translate(meaning)).strip()
            repaired_candidate = None
        except Exception as exc:
            raise TranslationReviewRequired("translation_provider_failed") from exc
        if not candidate:
            raise TranslationReviewRequired("empty_provider_translation")
        try:
            review = await independent_review(
                original, candidate, previous_turn, next_turn, meaning
            )
        except Exception as exc:
            raise TranslationReviewRequired("independent_review_unavailable") from exc
        if not isinstance(review, Review):
            raise TranslationReviewRequired("invalid_review_response")
        if review.approved is True:
            return ApprovedTranslation(candidate, attempt + 1, meaning)
        last_rejection_category = (
            "regional_spoken_language" if review.reason.startswith("regional_spoken_language_rejected:")
            else "semantic_or_fidelity"
        )
        if attempt < max_repairs:
            # Regional/register defects require target-language repair, not
            # another attempt to explain the English dialogue.
            if (repair_target_speech is not None and
                    review.reason.startswith("regional_spoken_language_rejected:")):
                try:
                    revised = await repair_target_speech(
                        original, candidate, meaning, review.reason[:350]
                    )
                    repaired_candidate = str(revised or "").strip()
                except Exception as exc:
                    raise TranslationReviewRequired("regional_speech_repair_failed") from exc
                if not repaired_candidate:
                    raise TranslationReviewRequired("empty_regional_speech_repair")
                continue
            try:
                revised = (await clarify_meaning(
                    original, previous_turn,
                    next_turn + "\\nReview feedback: " + review.reason[:350]
                )).strip()
            except Exception as exc:
                raise TranslationReviewRequired("meaning_repair_failed") from exc
            if not revised:
                raise TranslationReviewRequired("empty_meaning_repair")
            meaning = revised
    raise TranslationReviewRequired("TRANSLATION_REVIEW_REQUIRED:" + last_rejection_category)
