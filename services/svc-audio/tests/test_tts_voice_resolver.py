from __future__ import annotations

import unittest

from app.repos.tts_catalog_repo import TTSVoiceCandidate
from app.services.tts_voice_resolver import (
    TTSVoiceResolutionError,
    TTSVoiceResolutionRequest,
    TTSVoiceResolver,
)


def voice(
    *,
    name: str,
    gender: str = "female",
    is_default: bool = False,
    is_recommended: bool = False,
    is_native_fit: bool = True,
    quality_score=None,
):
    return TTSVoiceCandidate(
        voice_id=f"id-{name}",
        provider_code="provider-from-db",
        model_code="model-from-db",
        voice_name=name,
        home_locale="hi-IN",
        capability_locale="hi-IN",
        accent_code="",
        gender=gender,
        voice_type="Neural",
        is_default=is_default,
        supports_styles=True,
        is_native_fit=is_native_fit,
        is_recommended=is_recommended,
        quality_score=quality_score,
    )


class FakeCatalog:
    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.last_query = None

    async def list_voice_candidates(self, **kwargs):
        self.last_query = dict(kwargs)
        return list(self.candidates)


class TTSVoiceResolverTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_resolves_data_default_without_gender_bias(self):
        catalog = FakeCatalog(
            [
                voice(
                    name="voice-male",
                    gender="male",
                    is_default=True,
                ),
                voice(
                    name="voice-female",
                    gender="female",
                    is_default=False,
                ),
            ]
        )

        result = await TTSVoiceResolver(catalog).resolve(
            TTSVoiceResolutionRequest(
                provider_code="provider-from-db",
                model_code="model-from-db",
                canonical_locale="hi-IN",
            )
        )

        self.assertEqual(result.voice_name, "voice-male")

        # No implicit female/male preference was sent.
        self.assertIsNone(
            catalog.last_query["requested_gender"]
        )

    async def test_explicit_gender_is_forwarded(self):
        catalog = FakeCatalog(
            [
                voice(
                    name="voice-male",
                    gender="male",
                    is_default=True,
                )
            ]
        )

        await TTSVoiceResolver(catalog).resolve(
            TTSVoiceResolutionRequest(
                provider_code="provider-from-db",
                model_code="model-from-db",
                canonical_locale="hi-IN",
                requested_gender="male",
            )
        )

        self.assertEqual(
            catalog.last_query["requested_gender"],
            "male",
        )

    async def test_requested_voice_is_forwarded(self):
        catalog = FakeCatalog(
            [
                voice(
                    name="requested-voice",
                    is_default=False,
                )
            ]
        )

        result = await TTSVoiceResolver(catalog).resolve(
            TTSVoiceResolutionRequest(
                provider_code="provider-from-db",
                model_code="model-from-db",
                canonical_locale="hi-IN",
                requested_voice="requested-voice",
            )
        )

        self.assertEqual(
            result.voice_name,
            "requested-voice",
        )
        self.assertEqual(
            catalog.last_query["requested_voice"],
            "requested-voice",
        )

    async def test_no_candidate_fails_closed(self):
        resolver = TTSVoiceResolver(
            FakeCatalog([])
        )

        with self.assertRaisesRegex(
            TTSVoiceResolutionError,
            "no_eligible_tts_voice",
        ):
            await resolver.resolve(
                TTSVoiceResolutionRequest(
                    provider_code="provider-from-db",
                    model_code="model-from-db",
                    canonical_locale="hi-IN",
                )
            )

    async def test_requested_voice_missing_fails_closed(self):
        resolver = TTSVoiceResolver(
            FakeCatalog([])
        )

        with self.assertRaisesRegex(
            TTSVoiceResolutionError,
            "requested_voice_not_eligible",
        ):
            await resolver.resolve(
                TTSVoiceResolutionRequest(
                    provider_code="provider-from-db",
                    model_code="model-from-db",
                    canonical_locale="hi-IN",
                    requested_voice="missing",
                )
            )

    async def test_equal_top_rank_uses_repository_order(self):
        resolver = TTSVoiceResolver(
            FakeCatalog(
                [
                    voice(
                        name="voice-a",
                        is_default=False,
                        is_recommended=False,
                        quality_score=None,
                    ),
                    voice(
                        name="voice-b",
                        is_default=False,
                        is_recommended=False,
                        quality_score=None,
                    ),
                ]
            )
        )

        result = await resolver.resolve(
            TTSVoiceResolutionRequest(
                provider_code="provider-from-db",
                model_code="model-from-db",
                canonical_locale="hi-IN",
            )
        )

        self.assertEqual(result.voice_name, "voice-a")

    async def test_invalid_gender_rejected(self):
        resolver = TTSVoiceResolver(
            FakeCatalog([voice(name="voice-a")])
        )

        with self.assertRaisesRegex(
            TTSVoiceResolutionError,
            "invalid_voice_gender",
        ):
            await resolver.resolve(
                TTSVoiceResolutionRequest(
                    provider_code="provider-from-db",
                    model_code="model-from-db",
                    canonical_locale="hi-IN",
                    requested_gender="anything",
                )
            )


if __name__ == "__main__":
    unittest.main()
