from __future__ import annotations

import pytest

from app.workers import v3_scene_artifact_refresh as target


def test_fresh_artifact_video_url_ignores_persisted_top_level_url():
    payload = {
        "primary_video_url": "https://old.example/video.mp4?expired=yes",
        "share_url": "https://old.example/share.mp4?expired=yes",
        "artifacts": [
            {
                "kind": "video",
                "url": "https://desifacesstore.blob.core.windows.net/video-output/fresh.mp4?sig=fresh",
            }
        ],
    }

    assert target._fresh_artifact_video_url(payload).endswith("fresh.mp4?sig=fresh")


@pytest.mark.asyncio
async def test_refresh_terminal_child_urls_uses_full_job_artifact_and_preserves_lineage(monkeypatch):
    requests: list[str] = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "job_id": "job-1",
                "status": "succeeded",
                "artifacts": [
                    {
                        "kind": "video",
                        "url": "https://desifacesstore.blob.core.windows.net/video-output/new.mp4?sig=fresh",
                    }
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path: str):
            requests.append(path)
            return FakeResponse()

    monkeypatch.setattr(target.httpx, "AsyncClient", FakeClient)

    original = {
        "dialogue_turn_id": "turn-1",
        "fusion_job_id": "job-1",
        "sequence_no": 1,
        "status": "succeeded",
        "video_url": "https://desifacesstore.blob.core.windows.net/video-output/old.mp4?sig=expired",
    }

    refreshed = await target.refresh_terminal_child_urls_for_stitch([original])

    assert requests == ["/jobs/job-1"]
    assert len(refreshed) == 1
    child = refreshed[0]
    assert child["fusion_job_id"] == "job-1"
    assert child["dialogue_turn_id"] == "turn-1"
    assert child["video_url"].endswith("new.mp4?sig=fresh")
    assert child["video_url_refreshed_for_stitch"] is True
    assert child["video_url_refresh_source"] == "svc-fusion-full-status-artifact"


@pytest.mark.asyncio
async def test_refresh_terminal_child_urls_fails_closed_without_fresh_artifact(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "job_id": "job-1",
                "status": "succeeded",
                "primary_video_url": "https://old.example/video.mp4?expired=yes",
                "artifacts": [],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path: str):
            return FakeResponse()

    monkeypatch.setattr(target.httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError, match="fresh_artifact_url_missing"):
        await target.refresh_terminal_child_urls_for_stitch(
            [
                {
                    "dialogue_turn_id": "turn-1",
                    "fusion_job_id": "job-1",
                    "sequence_no": 1,
                    "status": "succeeded",
                    "video_url": "https://old.example/video.mp4?expired=yes",
                }
            ]
        )
