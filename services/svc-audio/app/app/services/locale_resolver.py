from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Sequence


class LocaleResolutionError(ValueError):
    """Deterministic locale-resolution failure."""


@dataclass(frozen=True)
class ResolvedLocale:
    locale: str
    translator_lang: Optional[str]
    tts_supported: bool
    translate_supported: bool
    display_name: Optional[str]
    native_name: Optional[str]
    resolution_source: str
    matched_key: str


class LocaleRepositoryProtocol(Protocol):
    async def get_enabled_locale(
        self,
        *,
        normalized_key: str,
    ) -> Optional[Dict[str, Any]]:
        ...

    async def get_enabled_alias(
        self,
        *,
        alias_key: str,
    ) -> Optional[Dict[str, Any]]:
        ...

    async def list_enabled_locales_for_language(
        self,
        *,
        language_code: str,
    ) -> Sequence[Dict[str, Any]]:
        ...


def normalize_locale_lookup_key(value: str) -> str:
    """
    Syntax normalization only.

    This function does NOT contain any country/language/locale mapping.
    Semantic resolution belongs entirely to database masterdata.
    """
    return str(value or "").strip().replace("_", "-").lower()


class LocaleResolver:
    """
    Resolve caller locale input against DB-backed masterdata.

    Resolution order:
      1. canonical tts_locales entry
      2. tts_locale_aliases entry targeting a canonical locale
      3. tts_locale_aliases entry targeting a language code
         - succeeds only when exactly one enabled canonical locale matches
         - otherwise fails as ambiguous

    No geography or language mappings are embedded here.
    """

    def __init__(self, repository: LocaleRepositoryProtocol):
        self.repository = repository

    @staticmethod
    def _resolved(
        row: Dict[str, Any],
        *,
        source: str,
        matched_key: str,
    ) -> ResolvedLocale:
        return ResolvedLocale(
            locale=str(row["locale"]),
            translator_lang=(
                str(row["translator_lang"])
                if row.get("translator_lang") is not None
                else None
            ),
            tts_supported=bool(row.get("tts_supported")),
            translate_supported=bool(row.get("translate_supported")),
            display_name=(
                str(row["display_name"])
                if row.get("display_name") is not None
                else None
            ),
            native_name=(
                str(row["native_name"])
                if row.get("native_name") is not None
                else None
            ),
            resolution_source=source,
            matched_key=matched_key,
        )

    async def resolve(self, value: str) -> ResolvedLocale:
        key = normalize_locale_lookup_key(value)

        if not key:
            raise LocaleResolutionError("missing_locale")

        direct = await self.repository.get_enabled_locale(
            normalized_key=key,
        )
        if direct:
            return self._resolved(
                direct,
                source="canonical",
                matched_key=key,
            )

        alias = await self.repository.get_enabled_alias(
            alias_key=key,
        )
        if not alias:
            raise LocaleResolutionError(
                f"unknown_locale:{key}"
            )

        target_locale = str(alias.get("locale") or "").strip()

        if target_locale:
            canonical = await self.repository.get_enabled_locale(
                normalized_key=normalize_locale_lookup_key(
                    target_locale
                ),
            )

            if not canonical:
                raise LocaleResolutionError(
                    "alias_target_locale_unavailable:"
                    f"{key}:{target_locale}"
                )

            return self._resolved(
                canonical,
                source=f"alias:{alias.get('alias_type') or 'alias'}",
                matched_key=key,
            )

        language_code = str(
            alias.get("language_code") or ""
        ).strip().lower()

        if not language_code:
            raise LocaleResolutionError(
                f"alias_target_missing:{key}"
            )

        candidates = list(
            await self.repository.list_enabled_locales_for_language(
                language_code=language_code,
            )
        )

        if not candidates:
            raise LocaleResolutionError(
                "no_locale_for_language:"
                f"{language_code}"
            )

        if len(candidates) > 1:
            locales = ",".join(
                sorted(
                    str(candidate.get("locale") or "")
                    for candidate in candidates
                )
            )

            raise LocaleResolutionError(
                "ambiguous_locale:"
                f"{language_code}:"
                f"{locales}"
            )

        return self._resolved(
            candidates[0],
            source=f"alias:{alias.get('alias_type') or 'alias'}",
            matched_key=key,
        )
