import unittest
from types import SimpleNamespace

from app.services.tts_provider_adapter import (
    TTSProviderSynthesisResult,
)
from app.services.tts_provider_executor import (
    TTSProviderExecutor,
)


class FakeAdapter:
    def __init__(self):
        self.request = None

    async def synthesize(self, request):
        self.request = request
        return TTSProviderSynthesisResult(
            provider_code=request.provider_code,
            model_code=request.model_code,
            voice_name=request.voice_name,
            audio_bytes=b"audio",
            content_type="audio/mpeg",
            extension="mp3",
            metadata={},
        )


class FakeRegistry:
    def __init__(self, adapter):
        self.adapter = adapter
        self.key = None

    def create(self, key):
        self.key = key
        return self.adapter


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_passes_through_unchanged(self):
        adapter = FakeAdapter()
        registry = FakeRegistry(adapter)

        plan = SimpleNamespace(
            adapter_key="sarvam",
            provider_code="sarvam",
            model_code="bulbul_v3",
            provider_model_id="bulbul:v3",
            voice_name="speaker-db",
            canonical_locale="or-IN",
            provider_locale_code="od-IN",
        )

        result = await TTSProviderExecutor(
            registry=registry
        ).synthesize(
            plan=plan,
            text="hello",
            output_format="mp3",
            rate=1.1,
        )

        self.assertEqual(registry.key, "sarvam")
        self.assertEqual(
            adapter.request.provider_code,
            "sarvam",
        )
        self.assertEqual(
            adapter.request.provider_model_id,
            "bulbul:v3",
        )
        self.assertEqual(
            adapter.request.voice_name,
            "speaker-db",
        )
        self.assertEqual(
            adapter.request.canonical_locale,
            "or-IN",
        )
        self.assertEqual(
            adapter.request.provider_locale_code,
            "od-IN",
        )
        self.assertEqual(adapter.request.rate, 1.1)
        self.assertEqual(result.audio_bytes, b"audio")


if __name__ == "__main__":
    unittest.main()
