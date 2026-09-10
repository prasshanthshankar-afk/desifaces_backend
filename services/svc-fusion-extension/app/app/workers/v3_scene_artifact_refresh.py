from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import httpx

from app.workers import v3_scene_coordinator as coordinator


_TERMINAL_SUCCESS = {"succeeded", "success", "completed", "complete", "ready"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value or {})
    except Exception:
        return {}


def _fresh_artifact_video_url(payload: dict[str, Any]) -> str:
    """Use only freshly minted artifact URLs from svc-fusion full job status.

    Top-level primary/share URLs are persisted convenience fields and may contain an
    expired SAS token. The full job endpoint rebuilds artifact views and re-mints
    Azure read SAS URLs from durable artifact storage lineage.
    """
    artifacts = list(_as_dict(payload).get("artifacts") or [])
    for raw in artifacts:
        artifact = _as_dict(raw)
        kind = _clean(artifact.get("kind")).lower()
        url = _clean(artifact.get("url"))
        if url and "video" in kind:
            return url
    return ""


async def refresh_terminal_child_urls_for_stitch(
    children: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Refresh every successful child video URL immediately before scene stitch.

    A Fusion child job id is durable lineage; a signed Azure URL is not. Never allow
    the background finalizer to rely on a URL captured minutes or hours earlier.
    """
    ordered = sorted(
        [dict(item or {}) for item in children],
        key=lambda item: int(item.get("sequence_no") or 0),
    )
    if not ordered:
        return []

    semaphore = asyncio.Semaphore(
        max(1, min(coordinator._status_concurrency(), len(ordered)))
    )
    timeout = httpx.Timeout(45.0, connect=10.0)
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=32)

    async with httpx.AsyncClient(
        base_url=coordinator._fusion_base_url(),
        timeout=timeout,
        limits=limits,
    ) as client:

        async def refresh(raw: dict[str, Any]) -> dict[str, Any]:
            item = dict(raw or {})
            turn_id = _clean(item.get("dialogue_turn_id"))
            job_id = _clean(item.get("fusion_job_id"))
            state = _clean(item.get("status")).lower()
            if state not in _TERMINAL_SUCCESS:
                raise RuntimeError(
                    f"v3_scene_stitch_child_not_terminal:{turn_id or 'unknown'}:{state or 'unknown'}"
                )
            if not job_id:
                raise RuntimeError(
                    f"v3_scene_stitch_child_job_id_missing:{turn_id or 'unknown'}"
                )

            async with semaphore:
                response = await client.get(f"/jobs/{job_id}")
            response.raise_for_status()
            payload = dict(response.json() or {})

            live_state = _clean(payload.get("status")).lower()
            if live_state not in _TERMINAL_SUCCESS:
                raise RuntimeError(
                    f"v3_scene_stitch_child_live_state_changed:{turn_id or 'unknown'}:{job_id}:{live_state or 'unknown'}"
                )

            fresh_url = _fresh_artifact_video_url(payload)
            if not fresh_url:
                raise RuntimeError(
                    f"v3_scene_stitch_fresh_artifact_url_missing:{turn_id or 'unknown'}:{job_id}"
                )

            item.update(
                {
                    "status": "succeeded",
                    "video_url": fresh_url,
                    "video_url_refreshed_for_stitch": True,
                    "video_url_refresh_source": "svc-fusion-full-status-artifact",
                    "video_url_refreshed_at": coordinator._utc_iso(),
                }
            )
            return item

        refreshed = await asyncio.gather(*(refresh(item) for item in ordered))

    refreshed.sort(key=lambda item: int(item.get("sequence_no") or 0))
    return refreshed


_ORIGINAL_FINALIZE_SCENE = coordinator._finalize_scene


async def _finalize_scene_with_fresh_artifact_urls(
    pool: Any,
    row: dict[str, Any],
    children: list[dict[str, Any]],
) -> None:
    refreshed = await refresh_terminal_child_urls_for_stitch(children)
    await coordinator._persist_children(
        pool,
        attempt_id=UUID(str(row["attempt_id"])),
        children=refreshed,
        phase="artifact_refresh",
    )
    await _ORIGINAL_FINALIZE_SCENE(pool, row, refreshed)


if not bool(getattr(coordinator, "_fresh_stitch_artifact_urls_installed", False)):
    coordinator._finalize_scene = _finalize_scene_with_fresh_artifact_urls
    coordinator._fresh_stitch_artifact_urls_installed = True


v3_scene_coordinator_loop = coordinator.v3_scene_coordinator_loop


__all__ = [
    "_fresh_artifact_video_url",
    "refresh_terminal_child_urls_for_stitch",
    "v3_scene_coordinator_loop",
]
