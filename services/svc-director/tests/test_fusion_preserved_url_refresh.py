from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from app.fusion_execution_preserved_url_refresh import (
    _fresh_video_artifact_url,
    _refresh_child,
    refresh_latest_failed_attempt_preserved_urls,
)


class _FakeFusionClient:
    def __init__(self) -> None:
        self.status_calls: list[str] = []

    async def status(self, *, headers, job_id: str):
        self.status_calls.append(job_id)
        return {
            "job_id": job_id,
            "status": "succeeded",
            # Intentionally stale top-level URL: recovery must use the freshly
            # signed canonical artifact instead.
            "primary_video_url": "https://desifacesstore.blob.core.windows.net/video-output/stale.mp4?sig=expired",
            "artifacts": [
                {
                    "kind": "video",
                    "url": f"https://desifacesstore.blob.core.windows.net/video-output/{job_id}.mp4?sig=fresh",
                }
            ],
        }


class _FakeService:
    def __init__(self) -> None:
        self.fusion_client = _FakeFusionClient()


class _Acquire:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, row) -> None:
        self.row = row
        self.updated_metadata = None

    async def fetchrow(self, sql, *args):
        return self.row

    async def execute(self, sql, *args):
        assert "update public.v3_studio_stage_attempts" in sql.lower()
        self.updated_metadata = json.loads(args[1])
        return "UPDATE 1"


class _FakePool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_fresh_video_artifact_url_ignores_stale_top_level_url():
    payload = {
        "primary_video_url": "https://example.invalid/stale.mp4?sig=expired",
        "artifacts": [
            {"kind": "video", "url": "https://example.invalid/fresh.mp4?sig=fresh"}
        ],
    }
    assert _fresh_video_artifact_url(payload).endswith("fresh.mp4?sig=fresh")


def test_refresh_child_preserves_provider_job_lineage_and_replaces_only_signed_url():
    async def run():
        service = _FakeService()
        child = {
            "dialogue_turn_id": str(uuid4()),
            "fusion_job_id": "job-existing-001",
            "status": "succeeded",
            "video_url": "https://desifacesstore.blob.core.windows.net/video-output/old.mp4?sig=expired",
            "sequence_no": 1,
        }
        refreshed = await _refresh_child(
            service,
            child,
            headers={"Authorization": "Bearer test"},
            semaphore=asyncio.Semaphore(1),
        )
        assert refreshed["fusion_job_id"] == "job-existing-001"
        assert refreshed["status"] == "succeeded"
        assert refreshed["reused_from_prior_attempt"] is True
        assert refreshed["video_url_refreshed_for_stitch"] is True
        assert refreshed["video_url"].endswith("job-existing-001.mp4?sig=fresh")
        assert service.fusion_client.status_calls == ["job-existing-001"]

    asyncio.run(run())


def test_failed_attempt_refreshes_preserved_urls_without_creating_provider_jobs():
    async def run():
        attempt_id = uuid4()
        stage_run_id = uuid4()
        children = [
            {
                "dialogue_turn_id": str(uuid4()),
                "fusion_job_id": f"job-existing-{i:03d}",
                "status": "succeeded",
                "video_url": f"https://desifacesstore.blob.core.windows.net/video-output/old-{i}.mp4?sig=expired",
                "sequence_no": i,
            }
            for i in range(1, 4)
        ]
        conn = _FakeConn(
            {
                "attempt_id": attempt_id,
                "state": "failed",
                "stage_state": "failed",
                "metadata_json": {"children": children},
            }
        )
        pool = _FakePool(conn)
        service = _FakeService()

        count = await refresh_latest_failed_attempt_preserved_urls(
            service,
            pool,
            stage_run_id=stage_run_id,
            headers={"Authorization": "Bearer test"},
        )

        assert count == 3
        assert service.fusion_client.status_calls == [
            "job-existing-001",
            "job-existing-002",
            "job-existing-003",
        ]
        assert conn.updated_metadata["preserved_url_refresh"]["refreshed_count"] == 3
        assert conn.updated_metadata["preserved_url_refresh"]["new_provider_jobs"] == 0
        refreshed = conn.updated_metadata["children"]
        assert [item["fusion_job_id"] for item in refreshed] == [
            "job-existing-001",
            "job-existing-002",
            "job-existing-003",
        ]
        assert all("sig=fresh" in item["video_url"] for item in refreshed)

    asyncio.run(run())
