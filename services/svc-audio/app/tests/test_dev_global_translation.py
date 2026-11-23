"""Offline regression tests for DEV-only worldwide translation candidates."""
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.services import dev_global_translation as candidate


class FakePool:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_count = 0

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetch(self, sql, source, target):
        self.fetch_count += 1
        assert "public.tts_locales" in sql
        return [
            item for item in self.rows
            if item["locale"] in {source, target}
        ]


def locale(name, code, *, tts=True, selectable=True, enabled=True):
    return {
        "locale": name,
        "translator_lang": code,
        "is_enabled": enabled,
        "is_user_selectable": selectable,
        "translate_supported": True,
        "tts_supported": tts,
    }


class DevGlobalTranslationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        candidate._DISCOVERY_CACHE.clear()
        self.pool = FakePool([
            locale("en-US", "en"),
            locale("fr-FR", "fr"),
        ])
        self.env = patch.dict(os.environ, {
            "DF_RUNTIME_ENV": "dev",
            "DF_DEV_GLOBAL_TRANSLATION_ENABLED": "true",
            "AZURE_TRANSLATOR_KEY": "offline-fake-key",
            "AZURE_TRANSLATOR_REGION": "offline-test-region",
            "GOOGLE_CLOUD_TRANSLATION_API_KEY": "offline-fake-key",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    async def resolve(self, source="en-US", target="fr-FR"):
        async with __import__("httpx").AsyncClient() as client:
            return await candidate.resolve_dev_global_candidate(
                self.pool, client,
                source_locale=source,
                target_locale=target,
            )

    async def test_azure_provider_candidate_from_explicit_db_codes(self):
        with patch.object(
            candidate, "_provider_languages",
            new_callable=AsyncMock, return_value=frozenset({"en", "fr"})
        ):
            route = await self.resolve()
        self.assertEqual(route, {
            "provider_code": "azure_translator",
            "model_code": "text-translation-v3",
            "provider_source_code": "en",
            "provider_target_code": "fr",
        })

    async def test_google_candidate_if_azure_lacks_pair(self):
        async def catalog(client, provider):
            if provider == "azure_translator":
                return frozenset({"en"})
            return frozenset({"en", "fr"})

        with patch.object(
            candidate, "_provider_languages", side_effect=catalog
        ):
            route = await self.resolve()
        self.assertEqual(
            route["provider_code"], "google_cloud_translation"
        )

    async def test_no_candidate_if_provider_cannot_translate_pair(self):
        with patch.object(
            candidate, "_provider_languages",
            new_callable=AsyncMock, return_value=frozenset({"en"})
        ):
            with self.assertRaisesRegex(
                candidate.DevGlobalRouteUnavailable,
                "no_verified_provider_language_mapping"
            ):
                await self.resolve()

    async def test_production_never_uses_experimental_route(self):
        os.environ["DF_RUNTIME_ENV"] = "production"
        with self.assertRaisesRegex(
            candidate.DevGlobalRouteUnavailable,
            "dev_environment_required"
        ):
            await self.resolve()
        self.assertEqual(self.pool.fetch_count, 0)

    async def test_dev_feature_flag_is_required(self):
        os.environ["DF_DEV_GLOBAL_TRANSLATION_ENABLED"] = "false"
        with self.assertRaisesRegex(
            candidate.DevGlobalRouteUnavailable,
            "dev_global_translation_disabled"
        ):
            await self.resolve()
        self.assertEqual(self.pool.fetch_count, 0)

    async def test_missing_catalog_mapping_never_guessed(self):
        self.pool.rows[1]["translator_lang"] = ""
        with self.assertRaisesRegex(
            candidate.DevGlobalRouteUnavailable,
            "provider_locale_mapping_missing"
        ):
            await self.resolve()

    async def test_tts_ineligible_target_is_rejected(self):
        self.pool.rows[1]["tts_supported"] = False
        with self.assertRaisesRegex(
            candidate.DevGlobalRouteUnavailable,
            "language_not_end_to_end_eligible"
        ):
            await self.resolve()

    async def test_same_provider_code_is_not_dialect_translation(self):
        self.pool.rows[1]["translator_lang"] = "en"
        with self.assertRaisesRegex(
            candidate.DevGlobalRouteUnavailable,
            "regional_rewrite_not_certified"
        ):
            await self.resolve()

    async def test_successful_translation_remains_draft(self):
        with patch.object(
            candidate, "_provider_languages",
            new_callable=AsyncMock, return_value=frozenset({"en", "fr"})
        ), patch.object(
            candidate, "translate_via_configured_provider",
            new_callable=AsyncMock, return_value="Bonjour!"
        ) as translation, patch.object(
            candidate, "naturalize_provider_translation",
            new_callable=AsyncMock, return_value="Bonjour !"
        ) as naturalization:
            result = await candidate.translate_global_dev_draft(
                self.pool,
                text="Hello!", source_locale="en-US",
                target_locale="fr-FR", speaker_gender="female",
                original_context={"full_audio_script": "Maya: Hello!"},
            )
        self.assertEqual(
            result,
            ("Bonjour !", "azure_translator", "text-translation-v3")
        )
        self.assertEqual(translation.await_count, 1)
        self.assertEqual(naturalization.await_count, 1)


if __name__ == "__main__":
    unittest.main()
