from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional

from app.services.locale_resolver import (
    LocaleResolutionError,
    LocaleResolver,
    normalize_locale_lookup_key,
)


def locale_row(
    locale: str,
    language: str,
) -> Dict[str, Any]:
    return {
        "locale": locale,
        "translator_lang": language,
        "tts_supported": True,
        "translate_supported": True,
        "is_enabled": True,
        "display_name": None,
        "native_name": None,
        "meta_json": {},
    }


class FakeLocaleRepository:
    def __init__(
        self,
        *,
        locales: List[Dict[str, Any]],
        aliases: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.locales = locales
        self.aliases = aliases or {}

    async def get_enabled_locale(
        self,
        *,
        normalized_key: str,
    ) -> Optional[Dict[str, Any]]:
        for row in self.locales:
            key = normalize_locale_lookup_key(
                str(row["locale"])
            )
            if key == normalized_key:
                return row

        return None

    async def get_enabled_alias(
        self,
        *,
        alias_key: str,
    ) -> Optional[Dict[str, Any]]:
        return self.aliases.get(alias_key)

    async def list_enabled_locales_for_language(
        self,
        *,
        language_code: str,
    ) -> List[Dict[str, Any]]:
        return [
            row
            for row in self.locales
            if str(
                row.get("translator_lang") or ""
            ).lower()
            == language_code
        ]


class LocaleResolverTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_canonical_locale_is_syntax_normalized(self):
        repo = FakeLocaleRepository(
            locales=[locale_row("hi-IN", "hi")]
        )

        result = await LocaleResolver(repo).resolve("HI_in")

        self.assertEqual(result.locale, "hi-IN")
        self.assertEqual(
            result.resolution_source,
            "canonical",
        )

    async def test_language_name_alias_resolves_from_data(self):
        repo = FakeLocaleRepository(
            locales=[locale_row("hi-IN", "hi")],
            aliases={
                "hindi": {
                    "alias_key": "hindi",
                    "locale": None,
                    "language_code": "hi",
                    "alias_type": "display_name",
                }
            },
        )

        result = await LocaleResolver(repo).resolve("Hindi")

        self.assertEqual(result.locale, "hi-IN")
        self.assertEqual(
            result.resolution_source,
            "alias:display_name",
        )

    async def test_legacy_alias_is_data_driven(self):
        repo = FakeLocaleRepository(
            locales=[locale_row("hi-IN", "hi")],
            aliases={
                "india": {
                    "alias_key": "india",
                    "locale": None,
                    "language_code": "hi",
                    "alias_type": "legacy_compat",
                }
            },
        )

        result = await LocaleResolver(repo).resolve("india")

        self.assertEqual(result.locale, "hi-IN")

    async def test_language_alias_fails_when_ambiguous(self):
        repo = FakeLocaleRepository(
            locales=[
                locale_row("en-US", "en"),
                locale_row("en-GB", "en"),
            ],
            aliases={
                "en": {
                    "alias_key": "en",
                    "locale": None,
                    "language_code": "en",
                    "alias_type": "language_code",
                }
            },
        )

        with self.assertRaisesRegex(
            LocaleResolutionError,
            r"ambiguous_locale:en:",
        ):
            await LocaleResolver(repo).resolve("en")

    async def test_unknown_alias_does_not_guess(self):
        repo = FakeLocaleRepository(
            locales=[locale_row("hi-IN", "hi")]
        )

        with self.assertRaisesRegex(
            LocaleResolutionError,
            r"unknown_locale:",
        ):
            await LocaleResolver(repo).resolve(
                "not-a-real-locale"
            )


if __name__ == "__main__":
    unittest.main()
