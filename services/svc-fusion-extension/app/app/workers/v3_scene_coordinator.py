from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from app.api.routes.v3_scene_pricing import (
    ScenePricingKey,
    ScenePricingReleaseIn,
    commit_scene_pricing,
    release_scene_pricing,
)
from app.api.routes.v3_scene_stitch import SceneStitchIn, stitch_scene
from app.db import get_db_pool
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore

logger = logging.getLogger("svc_fusion_extension.v3_scene_coordinator")
store = CanonicalStudioWorkflowStore()

_SUCCESS = {"succeeded", "success", "completed", "complete", "ready"}
_FAILED = {"failed", "cancelled", "canceled", "blocked"}


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


def _enabled() -> bool:
    return _clean(os.getenv("DF_V3_SCENE_COORDINATOR_ENABLED", "0")).lower() in {
        "1", "true", "yes", "on",
    }


def _poll_seconds() -> float:
    try:
        return max(0.5, min(30.0, float(os.getenv("DF_V3_SCENE_COORDINATOR_POLL_SECONDS", "2"))))
    except Exception:
        return 2.0


def _batch_size() -> int:
    try:
        return max(1, min(16, int(os.getenv("DF_V3_SCENE_COORDINATOR_BATCH_SIZE", "4"))))
    except Exception:
        return 4


def _status_concurrency() -> int:
    try:
        return max(1, min(64, int(os.getenv("DF_V3_SCENE_COORDINATOR_STATUS_CONCURRENCY", "32"))))
    except Exception:
        return 32


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fusion_base_url() -> str:
    return _clean(os.getenv("SVC_FUSION_BASE_URL", "http://svc-fusion:8002")).rstrip("/")


def _video_url(payload: dict[str, Any]) -> str:
    for key in ("primary_video_url", "final_video_url", "output_video_url", "share_url", "video_url"):
        value = _clean(payload.get(key))
        if value:
            return value
    for raw in list(payload.get("artifacts") or []):
        item = _as_dict(raw)
        value = _clean(item.get("url"))
        if value and "video" in _clean(item.get("kind")).lower():
            return value
    for raw in list(payload.get("artifacts") or []):
        value = _clean(_as_dict(raw).get("url"))
        if value:
            return value
    return ""


async def _candidate_rows(pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select
              s.stage_run_id,s.workflow_id,s.scene_id,s.state as stage_state,
              s.metadata_json as stage_metadata,
              w.account_id,w.owner_user_id,w.project_id,
              a.attempt_id,a.state as attempt_state,a.created_at as attempt_created_at,
              a.metadata_json as attempt_metadata
            from public.v3_studio_stage_runs s
            join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
            join lateral (
              select attempt_id,state,created_at,metadata_json
              from public.v3_studio_stage_attempts
              where stage_run_id=s.stage_run_id
              order by attempt_no desc
              limit 1
            ) a on true
            where s.stage_type='fusion'
              and s.scope_type='scene'
              and s.state='generating'
              and a.state in ('dispatching','running','succeeded')
              and coalesce(s.metadata_json #>> '{fusion_parent_pricing,state}','')
                    in ('reserved','commit_pending')
            order by s.updated_at,s.stage_run_id
            limit $1
            """,
            _batch_size(),
        )
    return [dict(row) for row in rows]


async def _try_lock(conn, stage_run_id: UUID) -> bool:
    return bool(
        await conn.fetchval(
            "select pg_try_advisory_lock(hashtextextended($1::text, 0))",
            str(stage_run_id),
        )
    )


async def _unlock(conn, stage_run_id: UUID) -> None:
    await conn.execute(
        "select pg_advisory_unlock(hashtextextended($1::text, 0))",
        str(stage_run_id),
    )


async def _refresh_children(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, min(_status_concurrency(), len(children) or 1)))
    timeout = httpx.Timeout(45.0, connect=10.0)
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=32)

    async with httpx.AsyncClient(base_url=_fusion_base_url(), timeout=timeout, limits=limits) as client:
        async def refresh(raw: dict[str, Any]) -> dict[str, Any]:
            item = dict(raw or {})
            job_id = _clean(item.get("fusion_job_id"))
            if not job_id:
                item["status"] = "failed"
                item["error"] = "missing_fusion_job_id"
                return item
            if _clean(item.get("status")).lower() in _SUCCESS and _clean(item.get("video_url")):
                return item
            try:
                async with semaphore:
                    response = await client.get(f"/jobs/{job_id}/status-light")
                response.raise_for_status()
                payload = dict(response.json() or {})
            except Exception as exc:
                item["poll_error"] = str(exc)[:1000]
                return item

            state = _clean(payload.get("status")).lower()
            item["status"] = state or _clean(item.get("status")).lower() or "unknown"
            item["provider_status"] = payload.get("provider_status")
            item["error_code"] = payload.get("error_code")
            item["error_message"] = payload.get("error_message")
            item.pop("poll_error", None)

            if state in _SUCCESS:
                url = _video_url(payload)
                if not url:
                    try:
                        async with semaphore:
                            full = await client.get(f"/jobs/{job_id}")
                        full.raise_for_status()
                        url = _video_url(dict(full.json() or {}))
                    except Exception as exc:
                        item["terminal_artifact_error"] = str(exc)[:1000]
                if url:
                    item["video_url"] = url
                    item["status"] = "succeeded"
                    item.setdefault("provider_terminal_observed_at", _utc_iso())
                else:
                    item["status"] = "finalizing"
            return item

        result = await asyncio.gather(*(refresh(item) for item in children))
    result.sort(key=lambda item: int(item.get("sequence_no") or 0))
    return result


async def _persist_children(pool, *, attempt_id: UUID, children: list[dict[str, Any]], phase: str) -> None:
    counts = {
        "total": len(children),
        "completed": sum(_clean(item.get("status")).lower() in _SUCCESS for item in children),
        "failed": sum(_clean(item.get("status")).lower() in _FAILED for item in children),
        "queued": sum(_clean(item.get("status")).lower() == "queued" for item in children),
    }
    counts["processing"] = max(0, counts["total"] - counts["completed"] - counts["failed"] - counts["queued"])
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update public.v3_studio_stage_attempts
            set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,
                updated_at=now()
            where attempt_id=$1
            """,
            attempt_id,
            json.dumps({
                "children": children,
                "background_coordinator": {
                    "enabled": True,
                    "phase": phase,
                    "last_reconciled_at": _utc_iso(),
                    **counts,
                },
            }),
        )


async def _release_failed_scene(pool, row: dict[str, Any], *, children: list[dict[str, Any]], reason: str) -> None:
    body = ScenePricingReleaseIn(
        project_id=UUID(str(row["project_id"])),
        workflow_id=UUID(str(row["workflow_id"])),
        stage_run_id=UUID(str(row["stage_run_id"])),
        reason=reason[:300],
    )
    release_error = None
    try:
        released = await release_scene_pricing(
            body=body,
            user_id=str(row["owner_user_id"]),
            pool=pool,
        )
        released_pricing = dict(released.pricing or {})
    except Exception as exc:
        release_error = str(exc)[:1200]
        released_pricing = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set state='failed',completed_at=coalesce(completed_at,now()),
                    error_code='fusion_child_failed',error_message=$2,
                    metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,
                    updated_at=now()
                where attempt_id=$1
                """,
                row["attempt_id"],
                reason[:4000],
                json.dumps({
                    "children": children,
                    "parent_pricing": released_pricing,
                    "parent_pricing_release_error": release_error,
                    "background_coordinator": {
                        "enabled": True,
                        "phase": "failed",
                        "failed_at": _utc_iso(),
                    },
                }),
            )
            await store.mark_failed(
                conn,
                stage_run_id=UUID(str(row["stage_run_id"])),
                error=reason,
            )


async def _bind_lineage(conn, row: dict[str, Any], children: list[dict[str, Any]]) -> None:
    workflow_id = UUID(str(row["workflow_id"]))
    stage_run_id = UUID(str(row["stage_run_id"]))
    participant_ids = {
        UUID(str(item["participant_id"]))
        for item in children
        if _clean(item.get("participant_id"))
    }
    turn_ids = {
        UUID(str(item["dialogue_turn_id"]))
        for item in children
        if _clean(item.get("dialogue_turn_id"))
    }
    face_rows = await conn.fetch(
        """
        select distinct on (participant_id) participant_id,stage_run_id
        from public.v3_studio_stage_runs
        where workflow_id=$1 and stage_type='face' and scope_type='participant'
          and state='approved' and participant_id=any($2::uuid[])
        order by participant_id,created_at desc
        """,
        workflow_id,
        list(participant_ids),
    ) if participant_ids else []
    audio_rows = await conn.fetch(
        """
        select distinct on (dialogue_turn_id) dialogue_turn_id,stage_run_id
        from public.v3_studio_stage_runs
        where workflow_id=$1 and stage_type='audio' and scope_type='dialogue_turn'
          and state='approved' and dialogue_turn_id=any($2::uuid[])
        order by dialogue_turn_id,created_at desc
        """,
        workflow_id,
        list(turn_ids),
    ) if turn_ids else []
    face_stage = {str(item["participant_id"]): UUID(str(item["stage_run_id"])) for item in face_rows}
    audio_stage = {str(item["dialogue_turn_id"]): UUID(str(item["stage_run_id"])) for item in audio_rows}

    seen: set[tuple[str, str]] = set()
    for item in children:
        participant_id = _clean(item.get("participant_id"))
        turn_id = _clean(item.get("dialogue_turn_id"))
        face_media = _clean(item.get("face_media_id"))
        audio_media = _clean(item.get("audio_media_id"))
        face_source = face_stage.get(participant_id)
        audio_source = audio_stage.get(turn_id)
        if face_media and face_source and ("face", face_media) not in seen:
            await store.bind_approved_input(
                conn,
                stage_run_id=stage_run_id,
                media_id=UUID(face_media),
                input_role="approved_speaker_face",
                source_stage_run_id=face_source,
            )
            seen.add(("face", face_media))
        if audio_media and audio_source and ("audio", audio_media) not in seen:
            await store.bind_approved_input(
                conn,
                stage_run_id=stage_run_id,
                media_id=UUID(audio_media),
                input_role="approved_dialogue_audio",
                source_stage_run_id=audio_source,
            )
            seen.add(("audio", audio_media))


async def _finalize_scene(pool, row: dict[str, Any], children: list[dict[str, Any]]) -> None:
    ordered = sorted(children, key=lambda item: int(item.get("sequence_no") or 0))
    segment_urls = [_clean(item.get("video_url")) for item in ordered]
    if not segment_urls or any(not value for value in segment_urls):
        return

    started_wall = _utc_iso()
    started = time.perf_counter()
    await _persist_children(pool, attempt_id=UUID(str(row["attempt_id"])), children=ordered, phase="scene_stitch")

    stitch_body = SceneStitchIn(
        project_id=UUID(str(row["project_id"])),
        workflow_id=UUID(str(row["workflow_id"])),
        stage_run_id=UUID(str(row["stage_run_id"])),
        attempt_id=UUID(str(row["attempt_id"])),
        segment_urls=segment_urls,
    )
    try:
        stitched = await stitch_scene(
            body=stitch_body,
            user_id=str(row["owner_user_id"]),
            pool=pool,
        )
    except Exception as exc:
        await _release_failed_scene(
            pool,
            row,
            children=ordered,
            reason=f"fusion_scene_stitch_failed:{str(exc)[:1200]}",
        )
        return

    stitch_ms = max(0, int((time.perf_counter() - started) * 1000))
    stitch_finished = _utc_iso()

    pricing_body = ScenePricingKey(
        project_id=UUID(str(row["project_id"])),
        workflow_id=UUID(str(row["workflow_id"])),
        stage_run_id=UUID(str(row["stage_run_id"])),
    )
    try:
        committed = await commit_scene_pricing(
            body=pricing_body,
            user_id=str(row["owner_user_id"]),
            pool=pool,
        )
        committed_pricing = dict(committed.pricing or {})
        if _clean(committed_pricing.get("state")).lower() != "committed":
            raise RuntimeError("fusion_parent_pricing_commit_not_canonical")
    except Exception as exc:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,
                    updated_at=now()
                where attempt_id=$1
                """,
                row["attempt_id"],
                json.dumps({
                    "children": ordered,
                    "parent_pricing_commit_pending": True,
                    "parent_pricing_commit_error": str(exc)[:1200],
                    "background_coordinator": {
                        "enabled": True,
                        "phase": "pricing_commit",
                        "stitch_started_at": started_wall,
                        "stitch_completed_at": stitch_finished,
                        "stitch_ms": stitch_ms,
                        "last_reconciled_at": _utc_iso(),
                    },
                }),
            )
        return

    media_id = UUID(str(stitched.media_id))
    async with pool.acquire() as conn:
        async with conn.transaction():
            media = await conn.fetchrow(
                "select id,account_id,project_id,user_id from public.media_assets where id=$1",
                media_id,
            )
            if not media:
                raise RuntimeError("fusion_background_final_media_missing")
            if UUID(str(media["account_id"])) != UUID(str(row["account_id"])):
                raise RuntimeError("fusion_background_final_media_account_mismatch")
            if media["project_id"] and UUID(str(media["project_id"])) != UUID(str(row["project_id"])):
                raise RuntimeError("fusion_background_final_media_project_mismatch")

            review_id = await store.attach_output(
                conn,
                stage_run_id=UUID(str(row["stage_run_id"])),
                media_id=media_id,
                output_role="scene_video_candidate",
            )
            await _bind_lineage(conn, row, ordered)
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set state='succeeded',media_id=$2,
                    completed_at=coalesce(completed_at,now()),
                    metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,
                    updated_at=now()
                where attempt_id=$1
                """,
                row["attempt_id"],
                media_id,
                json.dumps({
                    "children": ordered,
                    "stitched_video_url": stitched.video_url,
                    "parent_pricing": committed_pricing,
                    "parent_pricing_commit_pending": False,
                    "parent_pricing_commit_error": None,
                    "background_coordinator": {
                        "enabled": True,
                        "phase": "ready_for_review",
                        "stitch_started_at": started_wall,
                        "stitch_completed_at": stitch_finished,
                        "stitch_ms": stitch_ms,
                        "finalized_at": _utc_iso(),
                        "review_item_id": str(review_id),
                    },
                }),
            )
            await conn.execute(
                """
                update public.v3_studio_stage_runs
                set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,
                    updated_at=now()
                where stage_run_id=$1
                """,
                row["stage_run_id"],
                json.dumps({
                    "canonical_scene_video_media_id": str(media_id),
                    "background_finalization": {
                        "enabled": True,
                        "completed_at": _utc_iso(),
                        "stitch_ms": stitch_ms,
                    },
                }),
            )

    logger.info(
        "v3_scene_background_finalized stage_run_id=%s attempt_id=%s media_id=%s stitch_ms=%s",
        row["stage_run_id"], row["attempt_id"], media_id, stitch_ms,
    )


async def reconcile_one(pool, row: dict[str, Any]) -> None:
    stage_run_id = UUID(str(row["stage_run_id"]))
    async with pool.acquire() as lock_conn:
        if not await _try_lock(lock_conn, stage_run_id):
            return
        try:
            metadata = _as_dict(row.get("attempt_metadata"))
            children = [dict(item or {}) for item in list(metadata.get("children") or [])]
            if not children:
                return
            refreshed = await _refresh_children(children)
            await _persist_children(
                pool,
                attempt_id=UUID(str(row["attempt_id"])),
                children=refreshed,
                phase="video_generation",
            )
            if any(_clean(item.get("status")).lower() in _FAILED for item in refreshed):
                await _release_failed_scene(
                    pool,
                    row,
                    children=refreshed,
                    reason="one_or_more_child_fusion_jobs_failed",
                )
                return
            all_succeeded = bool(refreshed) and all(
                _clean(item.get("status")).lower() in _SUCCESS and _clean(item.get("video_url"))
                for item in refreshed
            )
            if all_succeeded:
                await _finalize_scene(pool, row, refreshed)
        finally:
            await _unlock(lock_conn, stage_run_id)


async def v3_scene_coordinator_loop() -> None:
    if not _enabled():
        logger.info("V3 scene coordinator disabled")
        return
    pool = await get_db_pool()
    logger.info(
        "V3 scene coordinator started poll_seconds=%s batch_size=%s status_concurrency=%s",
        _poll_seconds(), _batch_size(), _status_concurrency(),
    )
    while True:
        try:
            rows = await _candidate_rows(pool)
            if not rows:
                await asyncio.sleep(_poll_seconds())
                continue
            await asyncio.gather(*(reconcile_one(pool, row) for row in rows))
        except Exception:
            logger.exception("V3 scene coordinator iteration failed")
            await asyncio.sleep(_poll_seconds())
        else:
            await asyncio.sleep(0.25)


__all__ = ["v3_scene_coordinator_loop", "reconcile_one"]
