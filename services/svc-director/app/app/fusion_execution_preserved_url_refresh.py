from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .fusion_execution import SceneFusionBridgeError, _as_dict, _clean


_TERMINAL_SUCCESS = {"succeeded", "completed", "complete", "ready"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_video_artifact_url(status_payload: dict[str, Any]) -> str:
    """Return the freshly signed canonical video artifact URL from full job status.

    Do not trust top-level primary/share URLs here: they can be persisted signed URLs
    whose SAS has expired. svc-fusion's full status response re-signs Azure artifact
    URLs, so artifacts are the recovery authority.
    """
    artifacts = list(_as_dict(status_payload).get("artifacts") or [])
    for raw in artifacts:
        artifact = _as_dict(raw)
        kind = _clean(artifact.get("kind")).lower()
        url = _clean(artifact.get("url"))
        if url and "video" in kind:
            return url
    for raw in artifacts:
        artifact = _as_dict(raw)
        url = _clean(artifact.get("url"))
        if url:
            return url
    return ""


async def _refresh_child(
    service: Any,
    child: dict[str, Any],
    *,
    headers: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    item = dict(child or {})
    turn_id = _clean(item.get("dialogue_turn_id"))
    job_id = _clean(item.get("fusion_job_id"))
    state = _clean(item.get("status")).lower()

    if state not in _TERMINAL_SUCCESS:
        return item
    if not turn_id or not job_id:
        raise SceneFusionBridgeError(
            f"fusion_preserved_child_lineage_missing:{turn_id or 'unknown'}"
        )

    # Recovery needs a newly signed canonical artifact URL. The production pooled
    # client's status() method intentionally uses /status-light for routine polling;
    # that endpoint does not load artifact rows or mint fresh SAS URLs. Use
    # status_full() when available so svc-fusion rebuilds the artifact view and
    # re-signs Azure Blob artifacts. Fall back only for non-pooled/test clients.
    status_full = getattr(service.fusion_client, "status_full", None)
    async with semaphore:
        if callable(status_full):
            status_payload = await status_full(
                headers=headers,
                job_id=job_id,
            )
        else:
            status_payload = await service.fusion_client.status(
                headers=headers,
                job_id=job_id,
            )

    live_state = _clean(status_payload.get("status")).lower()
    if live_state not in _TERMINAL_SUCCESS:
        raise SceneFusionBridgeError(
            f"fusion_preserved_child_not_succeeded:{turn_id}:{job_id}:{live_state or 'unknown'}"
        )

    fresh_url = _fresh_video_artifact_url(status_payload)
    if not fresh_url:
        raise SceneFusionBridgeError(
            f"fusion_preserved_child_fresh_artifact_url_missing:{turn_id}:{job_id}"
        )

    item.update(
        {
            "status": "succeeded",
            "video_url": fresh_url,
            "reused_from_prior_attempt": True,
            "video_url_refreshed_for_stitch": True,
            "video_url_refresh_source": "svc-fusion-full-status-artifact",
            "video_url_refreshed_at": _utc_iso(),
        }
    )
    return item


async def refresh_latest_failed_attempt_preserved_urls(
    service: Any,
    pool: Any,
    *,
    stage_run_id: UUID,
    headers: dict[str, str],
) -> int:
    """Refresh ephemeral SAS URLs before any retry parent reservation is created.

    Successful provider jobs are durable recovery lineage; their signed URLs are not.
    This function updates only the latest failed attempt's successful child URL fields
    so the existing retry implementation can preserve the same provider jobs while
    stitching from freshly signed canonical artifacts.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select a.attempt_id,a.state,a.metadata_json,s.state as stage_state
            from public.v3_studio_stage_attempts a
            join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
            where a.stage_run_id=$1
            order by a.attempt_no desc
            limit 1
            """,
            stage_run_id,
        )

    if not row:
        return 0
    if _clean(row["stage_state"]).lower() != "failed" or _clean(row["state"]).lower() != "failed":
        return 0

    metadata = _as_dict(row["metadata_json"])
    children = [dict(raw or {}) for raw in list(metadata.get("children") or [])]
    targets = [
        child
        for child in children
        if _clean(child.get("status")).lower() in _TERMINAL_SUCCESS
    ]
    if not targets:
        return 0

    limit = max(1, min(32, len(targets)))
    semaphore = asyncio.Semaphore(limit)
    refreshed_targets = await asyncio.gather(
        *(
            _refresh_child(
                service,
                child,
                headers=headers,
                semaphore=semaphore,
            )
            for child in targets
        )
    )
    refreshed_by_turn = {
        _clean(item.get("dialogue_turn_id")): item for item in refreshed_targets
    }

    merged: list[dict[str, Any]] = []
    refreshed_count = 0
    for child in children:
        turn_id = _clean(child.get("dialogue_turn_id"))
        replacement = refreshed_by_turn.get(turn_id)
        if replacement is not None:
            merged.append(replacement)
            refreshed_count += 1
        else:
            merged.append(child)

    metadata["children"] = merged
    metadata["preserved_url_refresh"] = {
        "source": "svc-fusion-full-status-artifact",
        "refreshed_count": refreshed_count,
        "refreshed_at": _utc_iso(),
        "new_provider_jobs": 0,
    }

    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            update public.v3_studio_stage_attempts
            set metadata_json=$2::jsonb,updated_at=now()
            where attempt_id=$1 and state='failed'
            """,
            row["attempt_id"],
            json.dumps(metadata),
        )
    if str(result or "").upper() not in {"UPDATE 1", "UPDATE 1.0"}:
        raise SceneFusionBridgeError("fusion_preserved_child_url_refresh_persist_failed")
    return refreshed_count


def install_preserved_child_url_refresh(service_cls: type) -> None:
    """Install a pre-dispatch guard on the concrete parallel parent-priced service."""
    if bool(getattr(service_cls, "_preserved_child_url_refresh_installed", False)):
        return

    original_dispatch = service_cls.dispatch

    async def dispatch_with_fresh_preserved_urls(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        parent_confirmation: dict[str, Any],
        child_confirmations: list[dict[str, Any]],
        external_provider_ok: bool,
    ):
        # This executes before original_dispatch reserves the parent price.
        await refresh_latest_failed_attempt_preserved_urls(
            self,
            pool,
            stage_run_id=stage_run_id,
            headers=headers,
        )
        return await original_dispatch(
            self,
            pool,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
            parent_confirmation=parent_confirmation,
            child_confirmations=child_confirmations,
            external_provider_ok=external_provider_ok,
        )

    service_cls.dispatch = dispatch_with_fresh_preserved_urls
    service_cls._preserved_child_url_refresh_installed = True


__all__ = [
    "_fresh_video_artifact_url",
    "_refresh_child",
    "install_preserved_child_url_refresh",
    "refresh_latest_failed_attempt_preserved_urls",
]
