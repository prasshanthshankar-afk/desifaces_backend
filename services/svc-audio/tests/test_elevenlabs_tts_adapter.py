from __future__ import annotations

import unittest

from app.services.elevenlabs_tts_adapter import (
    ElevenLabsTTSAdapter,
)
from app.services.tts_provider_adapter import (
    TTSProviderAdapterError,
    TTSProviderSynthesisRequest,
)


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        content=b"audio-bytes",
    ):
        self.status_code = status_code
        self.content = content


class FakeClient:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    async def post(
        self,
        url,
        *,
        headers,
        params,
        json,
    ):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "params": dict(params),
                "json": dict(json),
            }
        )
        return self.response


def make_request(
    *,
    voice_name="voice/db-id",
    provider_model_id="eleven_v3",
    text="hello world",
    output_format="mp3",
):
    return TTSProviderSynthesisRequest(
        provider_code="elevenlabs",
        model_code="eleven_v3",
        provider_model_id=provider_model_id,
        voice_name=voice_name,
        canonical_locale="hi",
        provider_locale_code=None,
        text=text,
        output_format=output_format,
    )


class ElevenLabsTTSAdapterTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_success_uses_expected_contract(self):
        client = FakeClient()

        adapter = ElevenLabsTTSAdapter(
            client=client,
            api_key="unit-test-key",
        )

        result = await adapter.synthesize(
            make_request()
        )

        self.assertEqual(
            result.audio_bytes,
            b"audio-bytes",
        )
        self.assertEqual(
            result.content_type,
            "audio/mpeg",
        )
        self.assertEqual(
            result.extension,
            "mp3",
        )

        self.assertEqual(
            len(client.calls),
            1,
        )

        call = client.calls[0]

        self.assertEqual(
            call["url"],
            (
                "https://api.elevenlabs.io/"
                "v1/text-to-speech/"
                "voice%2Fdb-id"
            ),
        )

        self.assertEqual(
            call["params"]["output_format"],
            "mp3_44100_128",
        )

        self.assertEqual(
            call["json"]["text"],
            "hello world",
        )

        self.assertEqual(
            call["json"]["model_id"],
            "eleven_v3",
        )

        # Do not inject a provider language override yet.
        self.assertNotIn(
            "language_code",
            call["json"],
        )

        self.assertEqual(
            call["headers"]["xi-api-key"],
            "unit-test-key",
        )

    async def test_missing_credential_fails_before_http(self):
        client = FakeClient()

        adapter = ElevenLabsTTSAdapter(
            client=client,
            api_key="",
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "missing_provider_credential",
        ):
            await adapter.synthesize(
                make_request()
            )

        self.assertEqual(
            client.calls,
            [],
        )

    async def test_missing_model_id_fails_closed(self):
        client = FakeClient()

        adapter = ElevenLabsTTSAdapter(
            client=client,
            api_key="unit-test-key",
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "missing_provider_model_id",
        ):
            await adapter.synthesize(
                make_request(
                    provider_model_id=None
                )
            )

        self.assertEqual(
            client.calls,
            [],
        )

    async def test_http_error_is_sanitized(self):
        client = FakeClient(
            FakeResponse(
                status_code=422,
                content=b"provider-secret-body",
            )
        )

        adapter = ElevenLabsTTSAdapter(
            client=client,
            api_key="unit-test-key",
        )

        try:
            await adapter.synthesize(
                make_request()
            )
            self.fail("expected error")

        except TTSProviderAdapterError as exc:
            message = str(exc)

            self.assertEqual(
                message,
                "provider_http_error:elevenlabs:422",
            )

            self.assertNotIn(
                "provider-secret-body",
                message,
            )

    async def test_empty_audio_fails_closed(self):
        client = FakeClient(
            FakeResponse(
                status_code=200,
                content=b"",
            )
        )

        adapter = ElevenLabsTTSAdapter(
            client=client,
            api_key="unit-test-key",
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "provider_returned_empty_audio",
        ):
            await adapter.synthesize(
                make_request()
            )

    async def test_unknown_format_fails_closed(self):
        client = FakeClient()

        adapter = ElevenLabsTTSAdapter(
            client=client,
            api_key="unit-test-key",
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "unsupported_output_format",
        ):
            await adapter.synthesize(
                make_request(
                    output_format="unknown"
                )
            )

        self.assertEqual(
            client.calls,
            [],
        )


if __name__ == "__main__":
    unittest.main()
