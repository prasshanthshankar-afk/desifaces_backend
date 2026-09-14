from __future__ import annotations

import asyncio

from app.fusion_execution_orphan_recovery import _authoritative_child_status


class _FakeFusionClient:
    def __init__(self, light, full=None, *, full_raises: bool = False) -> None:
        self.light = dict(light)
        self.full = dict(full or {})
        self.full_raises = full_raises
        self.status_calls: list[str] = []
        self.status_full_calls: list[str] = []

    async def status(self, *, headers, job_id: str):
        self.status_calls.append(job_id)
        return dict(self.light)

    async def status_full(self, *, headers, job_id: str):
        self.status_full_calls.append(job_id)
        if self.full_raises:
            raise RuntimeError("full status unavailable")
        return dict(self.full)


def test_stale_queued_light_status_reuses_completed_full_status_artifact():
    async def run():
        client = _FakeFusionClient(
            {"status": "queued", "artifacts": []},
            {
                "status": "succeeded",
                "artifacts": [
                    {
                        "kind": "video",
                        "url": "https://example.invalid/completed.mp4?sig=fresh",
                    }
                ],
            },
        )
        state, video_url = await _authoritative_child_status(
            client,
            headers={"Authorization": "Bearer test"},
            job_id="job-completed",
            persisted_state="queued",
        )
        assert state == "succeeded"
        assert video_url.endswith("completed.mp4?sig=fresh")
        assert client.status_calls == ["job-completed"]
        assert client.status_full_calls == ["job-completed"]

    asyncio.run(run())


def test_genuinely_running_child_remains_running_and_is_not_reusable():
    async def run():
        client = _FakeFusionClient(
            {"status": "running", "artifacts": []},
            {"status": "running", "artifacts": []},
        )
        state, video_url = await _authoritative_child_status(
            client,
            headers={},
            job_id="job-running",
            persisted_state="queued",
        )
        assert state == "running"
        assert not video_url
        assert client.status_full_calls == ["job-running"]

    asyncio.run(run())


def test_terminal_failure_does_not_probe_full_status_or_get_reused():
    async def run():
        client = _FakeFusionClient({"status": "failed", "artifacts": []})
        state, video_url = await _authoritative_child_status(
            client,
            headers={},
            job_id="job-failed",
            persisted_state="queued",
        )
        assert state == "failed"
        assert not video_url
        assert client.status_full_calls == []

    asyncio.run(run())


def test_light_success_with_video_does_not_add_extra_full_status_call():
    async def run():
        client = _FakeFusionClient(
            {
                "status": "succeeded",
                "video_url": "https://example.invalid/light-success.mp4?sig=fresh",
            }
        )
        state, video_url = await _authoritative_child_status(
            client,
            headers={},
            job_id="job-light-success",
            persisted_state="queued",
        )
        assert state == "succeeded"
        assert video_url.endswith("light-success.mp4?sig=fresh")
        assert client.status_full_calls == []

    asyncio.run(run())


def test_full_status_failure_fails_closed_on_original_active_state():
    async def run():
        client = _FakeFusionClient(
            {"status": "queued", "artifacts": []},
            full_raises=True,
        )
        state, video_url = await _authoritative_child_status(
            client,
            headers={},
            job_id="job-unknown",
            persisted_state="queued",
        )
        assert state == "queued"
        assert not video_url
        assert client.status_full_calls == ["job-unknown"]

    asyncio.run(run())
