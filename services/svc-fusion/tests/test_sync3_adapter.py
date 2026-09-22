from __future__ import annotations

import asyncio

import pytest

from app.domain.models import FusionJobCreate, VoiceAudio
from app.services.providers.base import ProviderPrepareInput
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
