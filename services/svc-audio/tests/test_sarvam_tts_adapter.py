import base64
import unittest

from app.services.sarvam_tts_adapter import SarvamTTSAdapter
from app.services.tts_provider_adapter import (
    TTSProviderAdapterError,
    TTSProviderSynthesisRequest,
)


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body or {
            "request_id": "req-test",
            "audios": [
                base64.b64encode(b"audio").decode()
            ],
        }

    def json(self):
        return self.body


class Client:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []

    async def post(self, url, *, headers, json):
        self.calls.append((url, headers, json))
        return self.response


def req(**overrides):
    values = dict(
        provider_code="sarvam",
        model_code="bulbul_v3",
        provider_model_id="bulbul:v3",
        voice_name="speaker-from-db",
        canonical_locale="or-IN",
        provider_locale_code="od-IN",
        text="hello",
        output_format="mp3",
        rate=1.0,
    )
    values.update(overrides)
    return TTSProviderSynthesisRequest(**values)


class SarvamAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_contract(self):
        client = Client()

        result = await SarvamTTSAdapter(
            client=client,
            api_key="test-key",
        ).synthesize(req())

        self.assertEqual(result.audio_bytes, b"audio")
        self.assertEqual(result.content_type, "audio/mpeg")

        url, headers, body = client.calls[0]

        self.assertEqual(
            url,
            "https://api.sarvam.ai/text-to-speech",
        )
        self.assertEqual(
            headers["api-subscription-key"],
            "test-key",
        )
        self.assertEqual(
            body["target_language_code"],
            "od-IN",
        )
        self.assertEqual(body["model"], "bulbul:v3")
        self.assertEqual(
            body["speaker"],
            "speaker-from-db",
        )

    async def test_missing_credential(self):
        client = Client()

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "missing_provider_credential",
        ):
            await SarvamTTSAdapter(
                client=client,
                api_key="",
            ).synthesize(req())

        self.assertEqual(client.calls, [])

    async def test_missing_provider_locale(self):
        client = Client()

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "missing_provider_locale_code",
        ):
            await SarvamTTSAdapter(
                client=client,
                api_key="test-key",
            ).synthesize(
                req(provider_locale_code=None)
            )

        self.assertEqual(client.calls, [])

    async def test_invalid_pace_fails_before_http(self):
        client = Client()

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "unsupported_pace",
        ):
            await SarvamTTSAdapter(
                client=client,
                api_key="test-key",
            ).synthesize(
                req(rate=3.0)
            )

        self.assertEqual(client.calls, [])

    async def test_http_error_is_sanitized(self):
        client = Client(
            Response(
                status=422,
                body={"secret": "do-not-leak"},
            )
        )

        try:
            await SarvamTTSAdapter(
                client=client,
                api_key="test-key",
            ).synthesize(req())
            self.fail("expected error")
        except TTSProviderAdapterError as exc:
            self.assertEqual(
                str(exc),
                "provider_http_error:sarvam:422",
            )
            self.assertNotIn(
                "do-not-leak",
                str(exc),
            )

    async def test_invalid_base64_fails_closed(self):
        client = Client(
            Response(
                body={
                    "audios": ["not-valid-base64%%%"]
                }
            )
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "provider_invalid_audio_encoding",
        ):
            await SarvamTTSAdapter(
                client=client,
                api_key="test-key",
            ).synthesize(req())


if __name__ == "__main__":
    unittest.main()
