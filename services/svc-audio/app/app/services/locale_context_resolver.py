from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.repos.locale_context_repo import (
    LocaleContextCandidate,
    LocaleContextRepository,
)


class LocaleContextResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedLocaleContext:
    locale: str
    resolution_source: str
    match_score: Optional[float] = None
    matched_context_count: int = 0


class LocaleContextResolver:
    """
    Refine a GENERIC locale using explicit request context and
    SQL-backed locale context rules.

    Rules:
      * explicit regional locale is never rewritten
      * no context -> preserve generic locale
      * no matching DB rule -> preserve generic locale
      * one best complete match -> refine
      * unresolved top tie -> fail closed
    """

    def __init__(
        self,
        repository: LocaleContextRepository,
    ):
        self.repository = repository

    @staticmethod
    def _clean(
        value: Optional[str],
    ) -> Optional[str]:
        raw = str(value or "").strip()
        return raw or None

    async def resolve(
        self,
        *,
        canonical_locale: str,
        country_code: Optional[str] = None,
        region_code: Optional[str] = None,
        accent_code: Optional[str] = None,
        dialect_code: Optional[str] = None,
    ) -> ResolvedLocaleContext:
        locale = str(
            canonical_locale or ""
        ).strip()

        if not locale:
            raise LocaleContextResolutionError(
                "missing_canonical_locale"
            )

        facts = await self.repository.get_locale_facts(
            locale
        )

        if facts is None:
            raise LocaleContextResolutionError(
                f"locale_not_found:{locale}"
            )

        # An explicit regional locale is authoritative.
        # Separate country/region context must not rewrite an
        # explicitly selected regional locale.
        if facts.region_code or facts.country_code:
            return ResolvedLocaleContext(
                locale=facts.locale,
                resolution_source=(
                    "explicit_regional_locale"
                ),
            )

        language_code = str(
            facts.language_code or ""
        ).strip().lower()

        if not language_code:
            raise LocaleContextResolutionError(
                f"missing_locale_language:{locale}"
            )

        country = self._clean(country_code)
        region = self._clean(region_code)
        accent = self._clean(accent_code)
        dialect = self._clean(dialect_code)

        requested_context_count = sum(
            item is not None
            for item in (
                country,
                region,
                accent,
                dialect,
            )
        )

        if requested_context_count == 0:
            return ResolvedLocaleContext(
                locale=facts.locale,
                resolution_source=(
                    "generic_no_context"
                ),
            )

        candidates = (
            await self.repository.list_matching_candidates(
                language_code=language_code,
                country_code=country,
                region_code=region,
                accent_code=accent,
                dialect_code=dialect,
                requested_context_count=(
                    requested_context_count
                ),
            )
        )

        if not candidates:
            # Preserve generic locale.
            #
            # A future multilingual provider may legitimately support
            # the generic language through language-level capability.
            return ResolvedLocaleContext(
                locale=facts.locale,
                resolution_source=(
                    "generic_no_context_match"
                ),
            )

        chosen: LocaleContextCandidate = (
            candidates[0]
        )

        if len(candidates) > 1:
            second = candidates[1]

            if (
                chosen.match_score
                == second.match_score
                and chosen.matched_context_count
                == second.matched_context_count
            ):
                tied = ",".join(
                    sorted(
                        item.locale
                        for item in candidates
                        if (
                            item.match_score
                            == chosen.match_score
                            and
                            item.matched_context_count
                            == chosen.matched_context_count
                        )
                    )
                )

                raise LocaleContextResolutionError(
                    "ambiguous_locale_context:"
                    f"{language_code}:{tied}"
                )

        return ResolvedLocaleContext(
            locale=chosen.locale,
            resolution_source="context_refined",
            match_score=chosen.match_score,
            matched_context_count=(
                chosen.matched_context_count
            ),
        )
