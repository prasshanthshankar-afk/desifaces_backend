from __future__ import annotations

import asyncio
from typing import Any

from app.repos.artifacts_repo import ArtifactsRepo


class _Acquire:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn

    async def __aenter__(self) -> "_Conn":
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _Pool:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, artifact_row: dict[str, Any] | None, media_row: dict[str, Any] | None) -> None:
        self.artifact_row = artifact_row
        self.media_row = media_row
        self.calls: list[str] = []

    async def fetchrow(self, query: str, asset_id: str):
        self.calls.append(query)
        if "FROM public.artifacts" in query:
            return self.artifact_row
        if "FROM public.media_assets" in query:
            return self.media_row
        raise AssertionError(f"unexpected query: {query}")


def _resolve(artifact_row: dict[str, Any] | None, media_row: dict[str, Any] | None):
    conn = _Conn(artifact_row, media_row)
    repo = ArtifactsRepo(_Pool(conn))  # type: ignore[arg-type]
    result = asyncio.run(repo.get_artifact_by_id("9488cfd2-8583-4b10-9078-c8eddf7bcb40"))
    return result, conn.calls


def test_job_artifact_remains_first_choice() -> None:
    artifact = {
        "id": "artifact-id",
        "job_id": "job-id",
        "kind": "face_image",
        "url": "https://example.invalid/artifact.png",
        "source_store": "artifacts",
    }
    media = {
        "id": "media-id",
        "job_id": None,
        "kind": "face_image",
        "url": "https://example.invalid/media.png",
        "source_store": "media_assets",
    }

    result, calls = _resolve(artifact, media)

    assert result == artifact
    assert len(calls) == 1
    assert "FROM public.artifacts" in calls[0]


def test_media_asset_is_valid_reusable_fusion_input() -> None:
    media = {
        "id": "9488cfd2-8583-4b10-9078-c8eddf7bcb40",
        "job_id": None,
        "kind": "face_image",
        "url": "https://storage.example.invalid/face-output/face.png",
        "content_type": "image/png",
        "meta_json": {"storage_path": "face-output/face.png"},
        "source_store": "media_assets",
    }

    result, calls = _resolve(None, media)

    assert result == media
    assert len(calls) == 2
    assert "FROM public.artifacts" in calls[0]
    assert "FROM public.media_assets" in calls[1]
    assert result["url"].endswith("face.png")
    assert result["kind"] == "face_image"


def test_unknown_uuid_remains_unresolved() -> None:
    result, calls = _resolve(None, None)

    assert result is None
    assert len(calls) == 2
