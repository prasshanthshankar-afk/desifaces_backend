import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.services.dev101_natural_speech import (
    NaturalSpeechRejected,
    naturalize_provider_translation,
)


class FakeClient:
    def __init__(self, translated, status="completed"):
        self.translated = translated
        self.status = status
        self.request = None

    async def post(self, url, headers, json):
        self.request = json
        outer = self

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "status": outer.status,
                    "output": [{
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": __import__("json").dumps({
                                "translation": outer.translated
                            }, ensure_ascii=False),
                        }],
                    }],
                }

        return Response()


class NaturalSpeechTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Existing language-generation tests isolate the independent audit.
        self._audit_patch = patch(
            "app.services.dev101_translation_quality_gate.audit_translation",
            new_callable=AsyncMock,
        )
        self.audit_mock = self._audit_patch.start()
        self.addCleanup(self._audit_patch.stop)

    async def test_quality_rejection_blocks_approval(self):
        from app.services.dev101_translation_quality_gate import (
            TranslationQualityRejected,
        )

        self.audit_mock.side_effect = TranslationQualityRejected(
            "translation_quality_rejected"
        )

        with patch.dict(__import__("os").environ, {
            "OPENAI_API_KEY": "offline-test"
        }):
            with self.assertRaisesRegex(
                NaturalSpeechRejected, "quality_gate_rejected"
            ):
                await naturalize_provider_translation(
                    **self.arguments(),
                    client=FakeClient("Unverified draft"),
                )

    def arguments(self):
        return dict(
            original_text="I will make coffee.",
            provider_draft="मैं कॉफी मेक करूँगा।",
            source_locale="en-US",
            target_locale="hi-IN",
            speaker_gender="male",
            original_context={
                "full_audio_script": (
                    "Maya: I welcome customers.\n"
                    "Arjun: I will make coffee."
                ),
                "previous_turn": "Maya welcomes customers.",
                "next_turn": "We open tomorrow.",
                "scene_context": "Two partners opening a cafe.",
                "speaker_age": 32,
                "addressee_age": 58,
                "speaker_relationship": "business partners",
                "address_preference": "respectful",
            },
        )

    async def test_context_and_explicit_age(self):
        client = FakeClient("मैं कॉफ़ी बनाऊँगा।")
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-test"
        }):
            result = await naturalize_provider_translation(
                **self.arguments(), client=client
            )
        self.assertEqual(result, "मैं कॉफ़ी बनाऊँगा।")
        payload = json.loads(client.request["input"])
        self.assertEqual(payload["speaker_age"], "32")
        self.assertEqual(payload["addressee_age"], "58")
        self.assertEqual(
            payload["address_preference"], "respectful"
        )

    async def test_missing_age_is_not_invented(self):
        args = self.arguments()
        args["original_context"].pop("speaker_age")
        args["original_context"].pop("addressee_age")
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-test"
        }):
            client = FakeClient("मैं कॉफ़ी बनाऊँगा।")
            await naturalize_provider_translation(
                **args, client=client
            )
        payload = json.loads(client.request["input"])
        self.assertEqual(payload["speaker_age"], "")
        self.assertEqual(payload["addressee_age"], "")

    async def test_missing_script_rejected(self):
        args = self.arguments()
        args["original_context"] = {}
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-test"
        }):
            with self.assertRaisesRegex(
                NaturalSpeechRejected, "full_script_required"
            ):
                await naturalize_provider_translation(
                    **args, client=FakeClient("unused")
                )

    async def test_missing_credential_rejected(self):
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(
                NaturalSpeechRejected, "credential_missing"
            ):
                await naturalize_provider_translation(
                    **self.arguments(),
                    client=FakeClient("unused"),
                )

    async def test_incomplete_response_rejected(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-test"
        }):
            with self.assertRaisesRegex(
                NaturalSpeechRejected, "response_incomplete"
            ):
                await naturalize_provider_translation(
                    **self.arguments(),
                    client=FakeClient(
                        "unused", status="incomplete"
                    ),
                )

    async def test_generic_target_locale(self):
        args = self.arguments()
        args["target_locale"] = "fr-CA"
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-test"
        }):
            result = await naturalize_provider_translation(
                **args,
                client=FakeClient("Je préparerai le café."),
            )
        self.assertEqual(
            result, "Je préparerai le café."
        )
