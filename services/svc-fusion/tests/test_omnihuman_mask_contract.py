from __future__ import annotations

import asyncio

from app.services.providers.base import ProviderPrepareInput
import app.services.providers.omnihuman_adapter as omnihuman_module
from app.services.providers.omnihuman_adapter import OmniHumanAdapter


def test_omnihuman_prepare_passes_speaker_mask_and_motion_prompt(monkeypatch):
    adapter = OmniHumanAdapter()

    async def fake_upload(url: str, *, suffix_hint: str) -> str:
        if "mask" in url:
            return "https://fal.media/test-speaker-mask.png"
        if "audio" in url:
            return "https://fal.media/test-audio.mp3"
        return "https://fal.media/test-group.png"

    monkeypatch.setattr(adapter, "_upload_remote_file_to_fal", fake_upload)

    prepared = asyncio.run(
        adapter.prepare(
            ProviderPrepareInput(
                job_id="job-1",
                user_id="user-1",
                request_payload={
                    "video": {"duration_sec": 7},
                    "provider_options": {
                        "mask_url": "https://example.com/mask.png",
                        "resolution": "720p",
                        "turbo_mode": False,
                        "prompt": (
                            "Natural conversational performance with subtle torso movement, "
                            "small hand gestures, realistic breathing and attentive eye contact."
                        ),
                    },
                },
                resolved_face_url="https://example.com/group.png",
                resolved_audio_url="https://example.com/audio.mp3",
            )
        )
    )

    assert prepared.request_json["image_url"] == "https://fal.media/test-group.png"
    assert prepared.request_json["audio_url"] == "https://fal.media/test-audio.mp3"
    assert prepared.request_json["mask_url"] == "https://fal.media/test-speaker-mask.png"
    assert prepared.request_json["resolution"] == "720p"
    assert "hand gestures" in prepared.request_json["prompt"]
    assert prepared.submit_meta["mask_enabled"] is True


def test_omnihuman_prepare_generates_shared_scene_mask_from_mapping(monkeypatch):
    adapter = OmniHumanAdapter()

    async def fake_upload(url: str, *, suffix_hint: str) -> str:
        if "audio" in url:
            return "https://fal.media/test-audio.mp3"
        return "https://fal.media/test-group.png"

    monkeypatch.setattr(adapter, "_upload_remote_file_to_fal", fake_upload)
    monkeypatch.setattr(
        omnihuman_module.fal_client,
        "upload_file",
        lambda path: "https://fal.media/generated-speaker-mask.png",
    )

    prepared = asyncio.run(
        adapter.prepare(
            ProviderPrepareInput(
                job_id="job-shared",
                user_id="user-1",
                request_payload={
                    "provider_options": {
                        "conversation_mode": "shared_scene",
                        "active_speaker_coordinates": [1100, 320],
                        "shared_scene_dimensions": {"width": 1536, "height": 1024},
                        "all_speaker_coordinates": [[430, 320], [1100, 320]],
                        "prompt": "Visible natural hand gesture and subtle torso shift.",
                        "resolution": "720p",
                    },
                },
                resolved_face_url="https://example.com/group.png",
                resolved_audio_url="https://example.com/audio.mp3",
            )
        )
    )

    assert prepared.request_json["mask_url"] == "https://fal.media/generated-speaker-mask.png"
    assert prepared.submit_meta["generated_shared_scene_mask"] is True
    assert prepared.submit_meta["mask_enabled"] is True
