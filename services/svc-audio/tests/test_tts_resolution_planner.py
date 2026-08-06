from __future__ import annotations

import unittest
from dataclasses import dataclass

from app.services.tts_model_resolver import (
    ResolvedTTSModel,
)
from app.services.tts_resolution_planner import (
    TTSResolutionPlanError,
    TTSResolutionPlanRequest,
    TTSResolutionPlanner,
)
from app.services.tts_voice_resolver import (
    ResolvedTTSVoice,
)


@dataclass(frozen=True)
class FakeResolvedLocale:
    locale: str


class FakeLocaleResolver:
    def __init__(self, result=None, error=None):
        self.result = (
            result
            if result is not None
            else FakeResolvedLocale("hi-IN")
        )
        self.error = error
        self.last_value = None

    async def resolve(self, value):
        self.last_value = value

        if self.error:
            raise self.error

        return self.result


class FakeModelResolver:
    def __init__(self, result=None, error=None):
        self.error = error
        self.last_request = None

        self.result = result or ResolvedTTSModel(
            provider_code="provider-db",
            adapter_key="adapter-db",
            model_code="model-db",
            provider_model_id="native-model-db",

            canonical_locale="hi-IN",
            language_code="hi",

            provider_locale_code="hi-IN",
            provider_language_code=None,
            capability_scope="locale",

            quality_class="high",
            quality_score=0.91,

            max_input_chars=5000,

            routing_policy_code="quality-db",
            masterdata_revision=17,

            country_code=None,
            region_code=None,
            accent_code=None,
        )

    async def resolve(self, request):
        self.last_request = request

        if self.error:
            raise self.error

        return self.result


class FakeVoiceResolver:
    def __init__(self, result=None, error=None):
        self.error = error
        self.last_request = None

        self.result = result or ResolvedTTSVoice(
            voice_id="voice-id-db",

            provider_code="provider-db",
            model_code="model-db",

            voice_name="voice-db",
            canonical_locale="hi-IN",
            home_locale="hi-IN",
            accent_code="",

            gender="male",
            voice_type="Neural",

            is_default=True,
            is_native_fit=True,
            is_recommended=True,

            quality_score=0.95,
        )

    async def resolve(self, request):
        self.last_request = request

        if self.error:
            raise self.error

        return self.result


class TTSResolutionPlannerTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_composes_locale_model_and_voice(self):
        locale = FakeLocaleResolver()
        model = FakeModelResolver()
        voice = FakeVoiceResolver()

        planner = TTSResolutionPlanner(
            locale_resolver=locale,
            model_resolver=model,
            voice_resolver=voice,
        )

        result = await planner.resolve(
            TTSResolutionPlanRequest(
                requested_locale="Hindi",
                text_length=500,
                output_format="mp3",
                requested_gender="male",
                country_code="US",
                region_code="VA",
                accent_code="requested",
                requires_style=True,
            )
        )

        self.assertEqual(
            locale.last_value,
            "Hindi",
        )

        self.assertEqual(
            model.last_request.canonical_locale,
            "hi-IN",
        )

        self.assertEqual(
            voice.last_request.provider_code,
            "provider-db",
        )
        self.assertEqual(
            voice.last_request.model_code,
            "model-db",
        )
        self.assertEqual(
            voice.last_request.canonical_locale,
            "hi-IN",
        )
        self.assertEqual(
            voice.last_request.requested_gender,
            "male",
        )

        self.assertEqual(
            result.canonical_locale,
            "hi-IN",
        )
        self.assertEqual(
            result.provider_code,
            "provider-db",
        )
        self.assertEqual(
            result.model_code,
            "model-db",
        )
        self.assertEqual(
            result.voice_name,
            "voice-db",
        )
        self.assertEqual(
            result.masterdata_revision,
            17,
        )

        # Geography remains context, not provider policy.
        self.assertEqual(
            result.country_code,
            "US",
        )
        self.assertEqual(
            result.region_code,
            "VA",
        )

    async def test_explicit_locale_survives_resolution(self):
        locale = FakeLocaleResolver(
            result=FakeResolvedLocale("ta-IN")
        )

        model_result = ResolvedTTSModel(
            provider_code="provider-db",
            adapter_key="adapter-db",
            model_code="model-db",
            provider_model_id="native-db",

            canonical_locale="ta-IN",
            language_code="ta",

            provider_locale_code="ta-IN",
            provider_language_code=None,
            capability_scope="locale",

            quality_class="high",
            quality_score=None,

            max_input_chars=None,

            routing_policy_code="policy-db",
            masterdata_revision=4,

            country_code=None,
            region_code=None,
            accent_code=None,
        )

        voice_result = ResolvedTTSVoice(
            voice_id="v-ta",

            provider_code="provider-db",
            model_code="model-db",

            voice_name="voice-ta",
            canonical_locale="ta-IN",
            home_locale="ta-IN",
            accent_code="",

            gender="female",
            voice_type=None,

            is_default=True,
            is_native_fit=True,
            is_recommended=False,

            quality_score=None,
        )

        planner = TTSResolutionPlanner(
            locale_resolver=locale,
            model_resolver=FakeModelResolver(
                result=model_result
            ),
            voice_resolver=FakeVoiceResolver(
                result=voice_result
            ),
        )

        result = await planner.resolve(
            TTSResolutionPlanRequest(
                requested_locale="ta-IN",
                text_length=10,
                country_code="US",
            )
        )

        self.assertEqual(
            result.canonical_locale,
            "ta-IN",
        )

        # Physical/user country context does not rewrite ta-IN.
        self.assertEqual(
            result.country_code,
            "US",
        )

    async def test_locale_failure_is_fail_closed(self):
        planner = TTSResolutionPlanner(
            locale_resolver=FakeLocaleResolver(
                error=ValueError("unknown")
            ),
            model_resolver=FakeModelResolver(),
            voice_resolver=FakeVoiceResolver(),
        )

        with self.assertRaisesRegex(
            TTSResolutionPlanError,
            "locale_resolution_failed",
        ):
            await planner.resolve(
                TTSResolutionPlanRequest(
                    requested_locale="not-real",
                    text_length=10,
                )
            )

    async def test_model_failure_is_fail_closed(self):
        planner = TTSResolutionPlanner(
            locale_resolver=FakeLocaleResolver(),
            model_resolver=FakeModelResolver(
                error=ValueError("none")
            ),
            voice_resolver=FakeVoiceResolver(),
        )

        with self.assertRaisesRegex(
            TTSResolutionPlanError,
            "model_resolution_failed",
        ):
            await planner.resolve(
                TTSResolutionPlanRequest(
                    requested_locale="hi-IN",
                    text_length=10,
                )
            )

    async def test_voice_failure_is_fail_closed(self):
        planner = TTSResolutionPlanner(
            locale_resolver=FakeLocaleResolver(),
            model_resolver=FakeModelResolver(),
            voice_resolver=FakeVoiceResolver(
                error=ValueError("none")
            ),
        )

        with self.assertRaisesRegex(
            TTSResolutionPlanError,
            "voice_resolution_failed",
        ):
            await planner.resolve(
                TTSResolutionPlanRequest(
                    requested_locale="hi-IN",
                    text_length=10,
                )
            )

    async def test_provider_mismatch_is_rejected(self):
        voice_result = ResolvedTTSVoice(
            voice_id="voice-id",

            provider_code="different-provider",
            model_code="model-db",

            voice_name="voice-db",
            canonical_locale="hi-IN",
            home_locale="hi-IN",
            accent_code="",

            gender="male",
            voice_type=None,

            is_default=True,
            is_native_fit=True,
            is_recommended=True,

            quality_score=None,
        )

        planner = TTSResolutionPlanner(
            locale_resolver=FakeLocaleResolver(),
            model_resolver=FakeModelResolver(),
            voice_resolver=FakeVoiceResolver(
                result=voice_result
            ),
        )

        with self.assertRaisesRegex(
            TTSResolutionPlanError,
            "provider_resolution_mismatch",
        ):
            await planner.resolve(
                TTSResolutionPlanRequest(
                    requested_locale="hi-IN",
                    text_length=10,
                )
            )

    async def test_model_mismatch_is_rejected(self):
        voice_result = ResolvedTTSVoice(
            voice_id="voice-id",

            provider_code="provider-db",
            model_code="different-model",

            voice_name="voice-db",
            canonical_locale="hi-IN",
            home_locale="hi-IN",
            accent_code="",

            gender="male",
            voice_type=None,

            is_default=True,
            is_native_fit=True,
            is_recommended=True,

            quality_score=None,
        )

        planner = TTSResolutionPlanner(
            locale_resolver=FakeLocaleResolver(),
            model_resolver=FakeModelResolver(),
            voice_resolver=FakeVoiceResolver(
                result=voice_result
            ),
        )

        with self.assertRaisesRegex(
            TTSResolutionPlanError,
            "model_resolution_mismatch",
        ):
            await planner.resolve(
                TTSResolutionPlanRequest(
                    requested_locale="hi-IN",
                    text_length=10,
                )
            )

    async def test_voice_locale_mismatch_is_rejected(self):
        voice_result = ResolvedTTSVoice(
            voice_id="voice-id",

            provider_code="provider-db",
            model_code="model-db",

            voice_name="voice-db",
            canonical_locale="en-IN",
            home_locale="en-IN",
            accent_code="",

            gender="male",
            voice_type=None,

            is_default=True,
            is_native_fit=True,
            is_recommended=True,

            quality_score=None,
        )

        planner = TTSResolutionPlanner(
            locale_resolver=FakeLocaleResolver(),
            model_resolver=FakeModelResolver(),
            voice_resolver=FakeVoiceResolver(
                result=voice_result
            ),
        )

        with self.assertRaisesRegex(
            TTSResolutionPlanError,
            "voice_locale_resolution_mismatch",
        ):
            await planner.resolve(
                TTSResolutionPlanRequest(
                    requested_locale="hi-IN",
                    text_length=10,
                )
            )


if __name__ == "__main__":
    unittest.main()
