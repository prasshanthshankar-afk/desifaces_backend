from __future__ import annotations

import asyncio

import pytest

from app.domain.models import FusionJobCreate, VoiceAudio
from app.services.providers.base import ProviderPrepareInput
import app.services.providers.sync3_adapter as sync3_module
from app.services.providers.sync3_adapter import Sync3Adapter, Sync3AdapterError


def test_sync3_provider_is_accepted_by_fusion_contract():
    req = FusionJobCreate(
        provider="sync3",
        face_image_url="https://example.com/group.jpg",
        voice_audio=VoiceAudio(audio_url="https://example.com/turn.wav"),
        provider_options={"active_speaker_coordinates": [512, 384]},
    )
    assert req.provider == "sync3"


def test_sync3_prepare_builds_multi_face_manual_speaker_request():
    adapter = Sync3Adapter()
    prepared = asyncio.run(
        adapter.prepare(
            ProviderPrepareInput(
                job_id="job-1",
                user_id="user-1",
                request_payload={
                    "provider_options": {
                        "active_speaker_coordinates": [512, 384],
                    }
                },
                resolved_face_url="https://example.com/group.jpg",
                resolved_audio_url="https://example.com/turn.wav",
            )
        )
    )

    assert prepared.provider_name == "sync3"
    assert prepared.request_json == {
        "model": "sync-3",
        "input": [
            {"type": "image", "url": "https://example.com/group.jpg"},
            {"type": "audio", "url": "https://example.com/turn.wav"},
        ],
        "options": {
            "active_speaker_detection": {
                "auto_detect": False,
                "frame_number": 0,
                "coordinates": [512, 384],
            }
        },
    }


def test_sync3_prepare_requires_explicit_speaker_coordinates():
    adapter = Sync3Adapter()
    with pytest.raises(Sync3AdapterError, match="SYNC3_ACTIVE_SPEAKER_COORDINATES_REQUIRED"):
        asyncio.run(
            adapter.prepare(
                ProviderPrepareInput(
                    job_id="job-1",
                    user_id="user-1",
                    request_payload={},
                    resolved_face_url="https://example.com/group.jpg",
                    resolved_audio_url="https://example.com/turn.wav",
                )
            )
        )


def test_sync3_submit_waits_through_provider_concurrency_limit(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    responses = [
        FakeResponse(
            429,
            {
                "errorCode": "concurrency_limit_reached",
                "retryAfterSeconds": 20,
            },
        ),
        FakeResponse(201, {"id": "sync-job-1"}),
        FakeResponse(
            200,
            {
                "id": "sync-job-1",
                "status": "COMPLETED",
                "outputUrl": "https://example.com/out.mp4",
            },
        ),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return responses.pop(0)

        async def get(self, *args, **kwargs):
            return responses.pop(0)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(sync3_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(sync3_module.asyncio, "sleep", no_sleep)
    monkeypatch.setenv("SYNC_API_KEY", "test-key")

    async def scenario():
        adapter = Sync3Adapter()
        submitted = await adapter.submit(
            {
                "model": "sync-3",
                "input": [
                    {"type": "image", "url": "https://example.com/group.jpg"},
                    {"type": "audio", "url": "https://example.com/turn.wav"},
                ],
                "options": {
                    "active_speaker_detection": {
                        "auto_detect": False,
                        "frame_number": 0,
                        "coordinates": [100, 100],
                    }
                },
            },
            "idem-1",
        )
        assert submitted.provider_job_id == "sync-job-1"
        polled = await adapter.poll(submitted.provider_job_id)
        assert polled.status == "succeeded"
        assert polled.video_url == "https://example.com/out.mp4"

    asyncio.run(scenario())
    assert responses == []
