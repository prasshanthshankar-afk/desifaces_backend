import os
import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from fastapi import FastAPI
from jose import jwt

from app.api.routes import dev101_translation_preview as preview

USER_ID = "10000000-0000-4000-8000-000000000001"
URL = "/api/audio/dev101/director-translation-preview"


class FakeConnection:
    def __init__(self, exists=True):
        self.exists = exists
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        assert "core.users" in query
        assert isinstance(args[0], UUID)
        return 1 if self.exists else None


class FakePool:
    def __init__(self, exists=True):
        self.conn = FakeConnection(exists)

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


def payload():
    return {
        "source_locale": "en-US",
        "target_locale": "hi-IN",
        "scene_context": "Two colleagues discussing their new cafe",
        "turns": [
            {
                "turn_id": "1:1",
                "speaker": "Maya",
                "speaker_gender": "female",
                "text": "I cannot wait to open our cafe.",
            },
            {
                "turn_id": "1:2",
                "speaker": "Arjun",
                "speaker_gender": "male",
                "text": "Our customers will love it.",
            },
        ],
    }


def authorization():
    token = jwt.encode(
        {
            "sub": USER_ID,
            "iss": "desifaces",
            "aud": "desifaces_clients",
            "exp": int(time.time()) + 1800,
        },
        os.environ["JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": "Bearer " + token}


class TranslationPreviewTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(preview.router)
        self.pool = FakePool()

        async def pool_override():
            return self.pool

        self.app.dependency_overrides[
            preview.get_pool
        ] = pool_override

        self.transport = httpx.ASGITransport(app=self.app)

    async def post(self, body=None, headers=None):
        async with httpx.AsyncClient(
            transport=self.transport,
            base_url="http://dev101.test",
        ) as client:
            return await client.post(
                URL,
                json=payload() if body is None else body,
                headers=headers or {},
            )

    async def test_missing_auth_rejected(self):
        result = await self.post()
        self.assertEqual(result.status_code, 401)

    async def test_invalid_language_pair_rejected(self):
        body = payload()
        body["source_locale"] = "auto"

        with patch.object(
            preview, "selected_for_dev",
            return_value=True,
        ), patch.object(
            preview, "translate_approved",
            new_callable=AsyncMock,
        ) as translator:
            result = await self.post(
                body, authorization()
            )

        self.assertEqual(result.status_code, 422)
        translator.assert_not_awaited()

    async def test_duplicate_turn_ids_rejected(self):
        body = payload()
        body["turns"][1]["turn_id"] = "1:1"

        with patch.object(
            preview, "selected_for_dev",
            return_value=True,
        ), patch.object(
            preview, "translate_approved",
            new_callable=AsyncMock,
        ) as translator:
            result = await self.post(
                body, authorization()
            )

        self.assertEqual(result.status_code, 422)
        translator.assert_not_awaited()

    async def test_cohort_restriction(self):
        with patch.object(
            preview, "selected_for_dev",
            return_value=False,
        ), patch.object(
            preview, "translate_approved",
            new_callable=AsyncMock,
        ) as translator:
            result = await self.post(
                headers=authorization()
            )

        self.assertEqual(result.status_code, 403)
        translator.assert_not_awaited()

    async def test_service_identity_rejected(self):
        self.app.dependency_overrides[
            preview.get_current_claims
        ] = lambda: {
            "is_service": True,
            "token_type": "service",
        }
        result = await self.post()
        self.assertEqual(result.status_code, 403)

    async def test_non_dev_rejected(self):
        with patch.dict(
            os.environ, {"DF_RUNTIME_ENV": "production"}
        ):
            result = await self.post(
                headers=authorization()
            )

        self.assertEqual(result.status_code, 404)

    async def test_unapproved_direction_rejected(self):
        translator = AsyncMock(
            side_effect=preview.DevTranslationRejected(
                "no_approved_translation_direction"
            )
        )

        with patch.object(
            preview, "selected_for_dev",
            return_value=True,
        ), patch.object(
            preview, "translate_approved",
            translator,
        ):
            result = await self.post(
                headers=authorization()
            )

        self.assertEqual(result.status_code, 422)

    async def test_rejected_second_turn_has_actionable_feedback(self):
        from app.services.dev101_natural_speech import NaturalSpeechRejected
        from app.services.dev101_translation_quality_gate import (
            TranslationQualityRejected,
        )

        audit = TranslationQualityRejected(
            "translation_quality_rejected",
            failed_checks=("speaker_actions_preserved",),
            issue="The wrong person promised to return.",
        )
        speech = NaturalSpeechRejected(
            "natural_speech_quality_gate_rejected"
        )
        speech.__cause__ = audit
        failure = preview.DevTranslationRejected(
            "natural_speech_check_failed"
        )
        failure.__cause__ = speech
        translator = AsyncMock(side_effect=[
            ("नमस्ते", "sarvam", "mayura:v1"),
            failure,
        ])

        with patch.object(
            preview, "selected_for_dev", return_value=True,
        ), patch.object(
            preview, "translate_approved", translator,
        ):
            response = await self.post(headers=authorization())

        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["turn_id"], "1:2")
        self.assertEqual(detail["speaker"], "Arjun")
        self.assertEqual(
            detail["failed_checks"], ["speaker_actions_preserved"]
        )
        self.assertIn(
            "The wrong person promised to return.",
            detail["message"],
        )
        self.assertEqual(translator.await_count, 2)

    async def test_provider_failure_is_not_fallback(self):
        translator = AsyncMock(
            side_effect=httpx.RequestError(
                "simulated_provider_failure"
            )
        )

        with patch.object(
            preview, "selected_for_dev",
            return_value=True,
        ), patch.object(
            preview, "translate_approved",
            translator,
        ):
            result = await self.post(
                headers=authorization()
            )

        self.assertEqual(result.status_code, 502)
        self.assertEqual(translator.await_count, 1)

    async def test_authenticated_provider_draft(self):
        translator = AsyncMock(
            return_value=(
                "हमारे ग्राहक इसे पसंद करेंगे।",
                "sarvam",
                "mayura:v1",
            )
        )

        with patch.object(
            preview, "selected_for_dev",
            return_value=True,
        ), patch.object(
            preview, "translate_approved",
            translator,
        ):
            result = await self.post(
                headers=authorization()
            )

        self.assertEqual(
            result.status_code, 200,
            result.text,
        )
        data = result.json()

        self.assertFalse(data["approved"])
        self.assertEqual(data["status"], "pending_hitl")
        self.assertEqual(data["provider"], "sarvam")
        self.assertEqual(data["model"], "mayura:v1")
        self.assertEqual(len(data["turns"]), 2)
        self.assertEqual(
            [t["turn_id"] for t in data["turns"]],
            ["1:1", "1:2"],
        )
        self.assertEqual(
            [t["speaker"] for t in data["turns"]],
            ["Maya", "Arjun"],
        )
        self.assertEqual(
            translator.await_count, 2
        )

        first = translator.await_args_list[0].kwargs
        second = translator.await_args_list[1].kwargs

        self.assertEqual(
            first["source_locale"], "en-US"
        )
        self.assertEqual(
            first["target_locale"], "hi-IN"
        )
        self.assertEqual(
            first["speaker_gender"], "female"
        )
        self.assertEqual(
            second["speaker_gender"], "male"
        )

        context = first["original_context"]
        self.assertIn(
            "Arjun", context["full_audio_script"]
        )
        self.assertEqual(
            context["next_turn"],
            "Our customers will love it.",
        )

    async def test_user_must_exist(self):
        self.pool = FakePool(exists=False)

        with patch.object(
            preview, "selected_for_dev",
            return_value=True,
        ), patch.object(
            preview, "translate_approved",
            new_callable=AsyncMock,
        ) as translator:
            result = await self.post(
                headers=authorization()
            )

        self.assertEqual(result.status_code, 401)
        translator.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
