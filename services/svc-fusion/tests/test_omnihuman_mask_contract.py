from __future__ import annotations

import asyncio

from app.services.providers.base import ProviderPrepareInput
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
