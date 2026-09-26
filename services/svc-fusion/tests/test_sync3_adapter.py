from __future__ import annotations

import asyncio

import pytest

from app.domain.models import FusionJobCreate, VoiceAudio
from app.services.providers.base import ProviderPrepareInput
from app.services.providers.sync3_adapter import Sync3Adapter, Sync3AdapterError, _active_count, _provider_concurrency_limit, _provider_wait_seconds


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


def test_sync3_active_generation_count_accepts_provider_shapes():
    assert _active_count([]) == 0
    assert _active_count([{"id": "a"}]) == 1
    assert _active_count({"activeGenerations": 2}) == 2
    assert _active_count({"generations": [{"id": "a"}, {"id": "b"}]}) == 2


def test_sync3_concurrency_env_defaults_to_one(monkeypatch):
    monkeypatch.delenv("DF_SYNC3_PROVIDER_CONCURRENCY", raising=False)
    monkeypatch.delenv("DF_SYNC3_CONCURRENCY_WAIT_SECONDS", raising=False)
    assert _provider_concurrency_limit() == 1
    assert _provider_wait_seconds() == 900.0


def test_sync3_concurrency_env_is_bounded(monkeypatch):
    monkeypatch.setenv("DF_SYNC3_PROVIDER_CONCURRENCY", "99")
    monkeypatch.setenv("DF_SYNC3_CONCURRENCY_WAIT_SECONDS", "99999")
    assert _provider_concurrency_limit() == 16
    assert _provider_wait_seconds() == 3600.0
