from __future__ import annotations

import unittest

from app.services.azure_tts_adapter import (
    AzureTTSAdapter,
)
from app.services.tts_provider_adapter import (
    TTSProviderAdapterError,
    TTSProviderSynthesisRequest,
)


class FakeAzureService:
    def __init__(
        self,
        *,
        fail: bool = False,
        empty: bool = False,
    ):
        self.fail = fail
        self.empty = empty
        self.calls = []

    async def synthesize_mp3(
        self,
        *,
        ssml: str,
    ) -> bytes:
        self.calls.append(
            ("mp3", ssml)
        )

        if self.fail:
            raise RuntimeError(
                "fake provider detail"
            )

        if self.empty:
            return b""

        return b"fake-mp3"

    async def synthesize_wav(
        self,
        *,
        ssml: str,
    ) -> bytes:
        self.calls.append(
            ("wav", ssml)
        )

        if self.fail:
            raise RuntimeError(
                "fake provider detail"
            )

        if self.empty:
            return b""

        return b"fake-wav"


def request(
    *,
    output_format: str = "mp3",
    ssml: str | None = "<speak>hello</speak>",
):
    return TTSProviderSynthesisRequest(
        provider_code="provider-from-db",
        model_code="model-from-db",
        provider_model_id=None,
        voice_name="voice-from-db",
        canonical_locale="locale-from-db",
        provider_locale_code="provider-locale-db",
        text="hello",
        output_format=output_format,
        ssml=ssml,
    )


class AzureTTSAdapterTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_mp3_uses_existing_mp3_service_path(self):
        service = FakeAzureService()
        adapter = AzureTTSAdapter(
            service=service
        )

        result = await adapter.synthesize(
            request(output_format="mp3")
        )

        self.assertEqual(
            service.calls,
            [
                (
                    "mp3",
                    "<speak>hello</speak>",
                )
            ],
        )

        self.assertEqual(
            result.audio_bytes,
            b"fake-mp3",
        )
        self.assertEqual(
            result.content_type,
            "audio/mpeg",
        )
        self.assertEqual(
            result.extension,
            "mp3",
        )

        # Provider/model/voice identity comes from the already
        # resolved execution plan, not adapter routing rules.
        self.assertEqual(
            result.provider_code,
            "provider-from-db",
        )
        self.assertEqual(
            result.model_code,
            "model-from-db",
        )
        self.assertEqual(
            result.voice_name,
            "voice-from-db",
        )

    async def test_wav_uses_existing_wav_service_path(self):
        service = FakeAzureService()
        adapter = AzureTTSAdapter(
            service=service
        )

        result = await adapter.synthesize(
            request(output_format="wav")
        )

        self.assertEqual(
            service.calls[0][0],
            "wav",
        )
        self.assertEqual(
            result.audio_bytes,
            b"fake-wav",
        )
        self.assertEqual(
            result.content_type,
            "audio/wav",
        )
        self.assertEqual(
            result.extension,
            "wav",
        )

    async def test_missing_ssml_fails_closed(self):
        adapter = AzureTTSAdapter(
            service=FakeAzureService()
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "azure_adapter_requires_ssml",
        ):
            await adapter.synthesize(
                request(ssml=None)
            )

    async def test_unknown_output_format_fails_closed(self):
        adapter = AzureTTSAdapter(
            service=FakeAzureService()
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "unsupported_output_format",
        ):
            await adapter.synthesize(
                request(
                    output_format="anything"
                )
            )

    async def test_provider_exception_is_sanitized(self):
        adapter = AzureTTSAdapter(
            service=FakeAzureService(
                fail=True
            )
        )

        try:
            await adapter.synthesize(
                request()
            )
            self.fail(
                "expected provider failure"
            )

        except TTSProviderAdapterError as exc:
            message = str(exc)

            self.assertIn(
                "provider_synthesis_failed",
                message,
            )

            # Arbitrary provider exception content must not leak
            # through the adapter boundary.
            self.assertNotIn(
                "fake provider detail",
                message,
            )

    async def test_empty_audio_fails_closed(self):
        adapter = AzureTTSAdapter(
            service=FakeAzureService(
                empty=True
            )
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "provider_returned_empty_audio",
        ):
            await adapter.synthesize(
                request()
            )


if __name__ == "__main__":
    unittest.main()
