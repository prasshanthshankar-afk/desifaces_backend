import json
import os
import unittest
from unittest.mock import patch

from app.services.dev101_translation_quality_gate import (
    TranslationQualityRejected,
    audit_translation,
)


class FakeClient:
    def __init__(self, flags):
        self.flags = flags
        self.request = None

    async def post(self, url, headers, json):
        self.request = json
        flags = self.flags

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "status": "completed",
                    "output": [{
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": __import__("json").dumps(
                                flags
                            ),
                        }],
                    }],
                }

        return Response()


class QualityGateTests(unittest.IsolatedAsyncioTestCase):
    def flags(self, **changes):
        result = {
            "meaning_preserved": True,
            "grammar_correct": True,
            "speaker_actions_preserved": True,
            "register_appropriate": True,
            "issue": "",
        }
        result.update(changes)
        return result

    async def audit(self, client):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-test"
        }):
            await audit_translation(
                original_text="Our cafe opens tomorrow.",
                translated_text="Our cafe opens tomorrow.",
                source_locale="en-US",
                target_locale="hi-IN",
                context={"speaker_relationship": "business partners"},
                client=client,
            )

    async def test_valid_candidate_accepted(self):
        client = FakeClient(self.flags())
        await self.audit(client)
        payload = json.loads(client.request["input"])
        self.assertEqual(
            payload["original_text"],
            "Our cafe opens tomorrow.",
        )

    async def test_changed_venue_is_rejected(self):
        client = FakeClient(self.flags(
            meaning_preserved=False,
            issue="Cafe changed to generic shop",
        ))
        with self.assertRaisesRegex(
            TranslationQualityRejected,
            "translation_quality_rejected",
        ):
            await self.audit(client)

    async def test_bad_grammar_is_rejected(self):
        client = FakeClient(self.flags(
            grammar_correct=False,
            issue="Incorrect verb construction",
        ))
        with self.assertRaisesRegex(
            TranslationQualityRejected,
            "translation_quality_rejected",
        ):
            await self.audit(client)

    async def test_wrong_register_is_rejected(self):
        client = FakeClient(self.flags(
            register_appropriate=False,
            issue="Inappropriate form of address",
        ))
        with self.assertRaisesRegex(
            TranslationQualityRejected,
            "translation_quality_rejected",
        ):
            await self.audit(client)
