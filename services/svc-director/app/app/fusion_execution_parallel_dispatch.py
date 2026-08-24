from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .fusion_execution import (
    SceneFusionBridgeError,
    _as_dict,
    _clean,
    _compile_children,
    _latest_attempt,
    load_fusion_scene_context,
)
from .fusion_execution_orphan_recovery import (
    OrphanReconciledParentPricedSceneFusionExecutionService,
)
from .fusion_execution_resilient import _completed_children


_TERMINAL_SUCCESS = {"succeeded", "completed", "complete", "ready"}
_TERMINAL_FAILURE = {"failed", "canceled", "cancelled", "blocked"}
_ACTIVE = {"running", "processing", "submitted", "pending", "finalizing", "unknown", ""}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dispatch_limit() -> int:
    raw = os.getenv("DF_DIRECTOR_FUSION_DISPATCH_CONCURRENCY", "32")
    try:
        return max(1, min(64, int(raw)))
    except Exception:
        return 32


def _child_record(child: dict[str, Any], *, job_id: str, started_at: datetime, accepted_at: datetime) -> dict[str, Any]:
    out = {k: v for k, v in child.items() if k != "payload"}
    out.update(
        {
            "fusion_job_id": job_id,
            "status": "queued",
            "pricing_suppressed": True,
            "quote_id": None,
            "preview_fingerprint": None,
            "reused_from_prior_attempt": False,
            "dispatch_started_at": _iso(started_at),
            "dispatch_accepted_at": _iso(accepted_at),
            "dispatch_latency_ms": max(0, int((accepted_at - started_at).total_seconds() * 1000)),
        }
    )
    return out


def _progress_from_result(
    *,
    result: dict[str, Any],
    metadata: dict[str, Any],
    created_at: datetime | None,
) -> dict[str, Any]:
    children = [dict(item or {}) for item in list(result.get("children") or [])]
    total = len(children)
    completed = 0
    processing = 0
    queued = 0
    failed = 0
    reused = 0

    for child in children:
        state = _clean(child.get("status")).lower()
        if state in _TERMINAL_SUCCESS:
            completed += 1
        elif state == "queued":
            queued += 1
        elif state in _TERMINAL_FAILURE:
            failed += 1
        elif state in _ACTIVE:
            processing += 1
        else:
            processing += 1
        if bool(child.get("reused_from_prior_attempt")):
            reused += 1

    stage_state = _clean(result.get("stage_state")).lower()
    provider_state = _clean(result.get("provider_state")).lower()
    if stage_state in {"awaiting_review", "approved"} or provider_state == "succeeded":
        phase = "ready_for_review"
        progress_pct = 100.0
    elif total > 0 and completed >= total:
        phase = "scene_stitch"
        progress_pct = 94.0
    else:
        phase = "video_generation"
        progress_pct = round(min(90.0, (completed / total) * 90.0), 1) if total else 0.0

    perf = _as_dict(metadata.get("dispatch_performance"))
    started_at = _parse_dt(perf.get("dispatch_started_at")) or created_at
    elapsed_seconds = None
    estimated_remaining_seconds = None
    eta_confidence = "unavailable"

    if started_at is not None:
        elapsed_seconds = max(0, int((_utc_now() - started_at).total_seconds()))
        remaining = max(0, total - completed)
        if remaining == 0:
            estimated_remaining_seconds = 0 if phase == "ready_for_review" else None
            eta_confidence = "medium" if phase == "ready_for_review" else "low"
        elif completed > 0 and elapsed_seconds >= 10:
            throughput = completed / max(1.0, float(elapsed_seconds))
            if throughput > 0:
                estimated_remaining_seconds = max(1, int(math.ceil(remaining / throughput)))
                eta_confidence = "medium" if completed >= max(3, int(math.ceil(total * 0.2))) else "low"

    if phase == "ready_for_review":
        message = "Your scene is ready for review."
    elif phase == "scene_stitch":
        message = "All clips are ready. Assembling your scene now."
    else:
        message = (
            f"Creating {total} dialogue clips in parallel — {completed} ready, "
            f"{processing} processing, {queued} queued."
        )

    return {
        "execution_mode": "parallel",
        "phase": phase,
        "total_jobs": total,
        "completed_jobs": completed,
        "processing_jobs": processing,
        "queued_jobs": queued,
        "failed_jobs": failed,
        "reused_jobs": reused,
        "progress_pct": progress_pct,
        "elapsed_seconds": elapsed_seconds,
        "estimated_remaining_seconds": estimated_remaining_seconds,
        "estimated_completion_confidence": eta_confidence,
        "next_phase": "human_review" if phase == "ready_for_review" else "scene_stitch",
        "message": message,
        "dispatch_concurrency": int(perf.get("dispatch_concurrency") or 0),
        "max_parallel_dispatch_observed": int(perf.get("max_parallel_dispatch_observed") or 0),
        "first_child_submitted_at": perf.get("first_child_submitted_at"),
        "last_child_submitted_at": perf.get("last_child_submitted_at"),
        "dispatch_spread_ms": perf.get("dispatch_spread_ms"),
        "dispatch_elapsed_ms": perf.get("dispatch_elapsed_ms"),
    }


class ParallelOrphanReconciledParentPricedSceneFusionExecutionService(
    OrphanReconciledParentPricedSceneFusionExecutionService
):
    """Production V3 Fusion execution with real child fan-out.

    Independent dialogue renders are created concurrently after the single parent
    reservation is established and after the durable Director attempt exists. The
    parent remains the only billable entity; children retain canonical suppressed
    pricing. Successful partial dispatches are persisted so orphan/retry recovery can
    reuse them without another provider render.
    """

    async def dispatch(
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
        if not external_provider_ok:
            raise SceneFusionBridgeError("fusion_external_provider_consent_required")

        child_confirmation_by_turn = {
            _clean(item.get("dialogue_turn_id")): item
            for item in child_confirmations
            if _clean(item.get("dialogue_turn_id"))
        }

        async with pool.acquire() as conn:
            context = await load_fusion_scene_context(
                conn,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
            )
            await self._reconcile_orphan_children(conn, context=context, headers=headers)
            context = await load_fusion_scene_context(
                conn,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
            )
            prior_state = context.stage_state
            if prior_state not in {"pending", "ready", "failed", "rejected"}:
                latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                if prior_state == "generating" and latest:
                    return (
                        context,
                        UUID(str(latest["attempt_id"])),
                        int(latest["attempt_no"]),
                        str(latest["attempt_kind"]),
                        list(_as_dict(latest["metadata_json"]).get("children") or []),
                        _as_dict(_as_dict(latest["metadata_json"]).get("parent_pricing")),
                    )
                raise SceneFusionBridgeError(f"fusion_stage_not_dispatchable:{prior_state}")
            await self.store.assert_startable(conn, stage_run_id=stage_run_id)

            preserved: dict[str, dict[str, Any]] = {}
            if prior_state == "failed":
                prior_attempt = await _latest_attempt(conn, stage_run_id=stage_run_id)
                if prior_attempt:
                    preserved = _completed_children(_as_dict(prior_attempt["metadata_json"]))

            all_turn_ids = {str(turn.dialogue_turn_id) for turn in context.turns}
            expected_turns = all_turn_ids - set(preserved)
            if set(child_confirmation_by_turn) != expected_turns:
                raise SceneFusionBridgeError("fusion_child_confirmation_bundle_mismatch")
            for turn_id, item in child_confirmation_by_turn.items():
                if not _clean(item.get("request_nonce")):
                    raise SceneFusionBridgeError(f"fusion_request_nonce_required:{turn_id}")

        quote_id = _clean(parent_confirmation.get("quote_id"))
        preview_fingerprint = _clean(parent_confirmation.get("preview_fingerprint"))
        if not quote_id or not preview_fingerprint:
            raise SceneFusionBridgeError("fusion_parent_pricing_confirmation_required")

        parent_reserved = await self.parent_pricing.reserve(
            headers=headers,
            context=context,
            quote_id=quote_id,
            preview_fingerprint=preview_fingerprint,
        )
        reserved_pricing = _as_dict(parent_reserved.get("pricing"))
        if _clean(reserved_pricing.get("state")).lower() != "reserved":
            raise SceneFusionBridgeError("fusion_parent_pricing_not_reserved")
        if not _clean(reserved_pricing.get("reservation_id")):
            raise SceneFusionBridgeError("fusion_parent_pricing_reservation_id_missing")

        preserved_children = [
            preserved[turn_id]
            for turn_id in sorted(
                preserved,
                key=lambda value: int(preserved[value].get("sequence_no") or 0),
            )
        ]

        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    locked_state = _clean(
                        await conn.fetchval(
                            "select state from public.v3_studio_stage_runs where stage_run_id=$1 for update",
                            stage_run_id,
                        )
                    )
                    if locked_state == "generating":
                        latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                        if latest:
                            return (
                                context,
                                UUID(str(latest["attempt_id"])),
                                int(latest["attempt_no"]),
                                str(latest["attempt_kind"]),
                                list(_as_dict(latest["metadata_json"]).get("children") or []),
                                reserved_pricing,
                            )
                    if locked_state not in {"pending", "ready", "failed", "rejected"}:
                        raise SceneFusionBridgeError(f"fusion_stage_not_dispatchable:{locked_state}")

                    attempt_no = int(
                        await conn.fetchval(
                            "select coalesce(max(attempt_no),0)+1 from public.v3_studio_stage_attempts where stage_run_id=$1",
                            stage_run_id,
                        )
                        or 1
                    )
                    attempt_kind = "initial" if attempt_no == 1 else (
                        "regenerate" if prior_state == "rejected" else "retry"
                    )
                    row = await conn.fetchrow(
                        """
                        insert into public.v3_studio_stage_attempts(
                          stage_run_id,attempt_no,attempt_kind,state,provider_service,provider_job_ref,
                          pricing_quote_id,preview_fingerprint,metadata_json
                        ) values($1,$2,$3,'dispatching','svc-director',$4,$5,$6,$7::jsonb)
                        returning attempt_id
                        """,
                        stage_run_id,
                        attempt_no,
                        attempt_kind,
                        f"scene-fusion:{stage_run_id}:{attempt_no}",
                        quote_id,
                        preview_fingerprint,
                        json.dumps(
                            {
                                "children": preserved_children,
                                "parent_pricing": reserved_pricing,
                                "parent_pricing_confirmation": parent_confirmation,
                                "child_confirmations": child_confirmations,
                                "preserved_child_count": len(preserved_children),
                                "retry_scope": "failed_child_only" if preserved_children else attempt_kind,
                                "execution_mode": "parallel",
                            }
                        ),
                    )
                    attempt_id = UUID(str(row["attempt_id"]))
                    await self.store.mark_generating(conn, stage_run_id=stage_run_id)
                    await conn.execute(
                        """
                        update public.v3_studio_stage_runs
                        set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                        where stage_run_id=$1
                        """,
                        stage_run_id,
                        json.dumps(
                            {
                                "fusion_attempt_id": str(attempt_id),
                                "fusion_attempt_no": attempt_no,
                                "fusion_attempt_kind": attempt_kind,
                                "render_strategy": "dialogue_turn_segments_then_stitch",
                                "pricing_owner": "svc-fusion-extension",
                                "child_pricing_strategy": "internal_bill_to_parent",
                                "execution_mode": "parallel",
                            }
                        ),
                    )
        except Exception:
            try:
                await self.parent_pricing.release(
                    headers=headers,
                    context=context,
                    reason="director_attempt_creation_failed",
                )
            except Exception:
                pass
            raise

        if not expected_turns:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='running',metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    json.dumps(
                        {
                            "children": preserved_children,
                            "dispatch_outcome": "stitch_only_retry",
                            "parent_pricing": reserved_pricing,
                            "execution_mode": "parallel",
                        }
                    ),
                )
            return context, attempt_id, attempt_no, attempt_kind, preserved_children, reserved_pricing

        children = await _compile_children(
            context=context,
            face_client=self.face_client,
            audio_client=self.audio_client,
            headers=headers,
            external_provider_ok=external_provider_ok,
            request_nonce_by_turn={
                turn_id: _clean(item.get("request_nonce"))
                for turn_id, item in child_confirmation_by_turn.items()
            },
        )
        required_children = [
            child for child in children if child["dialogue_turn_id"] in expected_turns
        ]
        required_children.sort(key=lambda item: int(item.get("sequence_no") or 0))

        limit = max(1, min(_dispatch_limit(), len(required_children)))
        semaphore = asyncio.Semaphore(limit)
        counter_lock = asyncio.Lock()
        in_flight = 0
        max_in_flight = 0
        dispatch_started = _utc_now()

        async def create_one(child: dict[str, Any]):
            nonlocal in_flight, max_in_flight
            async with semaphore:
                started_at = _utc_now()
                async with counter_lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                try:
                    job_id = await self._create_internal_child(
                        headers=headers,
                        child=child,
                        stage_run_id=stage_run_id,
                    )
                finally:
                    async with counter_lock:
                        in_flight = max(0, in_flight - 1)
                accepted_at = _utc_now()
                return _child_record(
                    child,
                    job_id=job_id,
                    started_at=started_at,
                    accepted_at=accepted_at,
                )

        raw_results = await asyncio.gather(
            *(create_one(child) for child in required_children),
            return_exceptions=True,
        )
        dispatch_finished = _utc_now()

        successful_children: list[dict[str, Any]] = []
        failures: list[tuple[str, Exception]] = []
        for child, raw in zip(required_children, raw_results):
            if isinstance(raw, Exception):
                failures.append((child["dialogue_turn_id"], raw))
            else:
                successful_children.append(raw)

        successful_children.sort(key=lambda item: int(item.get("sequence_no") or 0))
        dispatched = list(preserved_children) + successful_children
        dispatched.sort(key=lambda item: int(item.get("sequence_no") or 0))

        starts = [
            _parse_dt(item.get("dispatch_started_at"))
            for item in successful_children
            if _parse_dt(item.get("dispatch_started_at")) is not None
        ]
        starts = [item for item in starts if item is not None]
        first_submit = min(starts) if starts else None
        last_submit = max(starts) if starts else None
        spread_ms = (
            max(0, int((last_submit - first_submit).total_seconds() * 1000))
            if first_submit is not None and last_submit is not None
            else 0
        )
        perf = {
            "execution_mode": "parallel",
            "dispatch_concurrency": limit,
            "max_parallel_dispatch_observed": max_in_flight,
            "dispatch_started_at": _iso(dispatch_started),
            "dispatch_finished_at": _iso(dispatch_finished),
            "first_child_submitted_at": _iso(first_submit) if first_submit else None,
            "last_child_submitted_at": _iso(last_submit) if last_submit else None,
            "dispatch_spread_ms": spread_ms,
            "dispatch_elapsed_ms": max(0, int((dispatch_finished - dispatch_started).total_seconds() * 1000)),
            "requested_children": len(required_children),
            "accepted_children": len(successful_children),
            "failed_children": len(failures),
            "preserved_children": len(preserved_children),
        }

        if failures:
            release_error = None
            try:
                await self.parent_pricing.release(
                    headers=headers,
                    context=context,
                    reason="fusion_child_dispatch_failed",
                )
            except Exception as release_exc:
                release_error = str(release_exc)[:1200]
            failure_text = ";".join(f"{turn}:{str(exc)[:500]}" for turn, exc in failures)
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='failed',completed_at=coalesce(completed_at,now()),
                        error_code='fusion_child_dispatch_failed',error_message=$2,
                        metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    failure_text[:4000],
                    json.dumps(
                        {
                            "children": dispatched,
                            "dispatch_outcome": "failed",
                            "dispatch_performance": perf,
                            "parent_pricing_release_error": release_error,
                            "execution_mode": "parallel",
                        }
                    ),
                )
                await self.store.mark_failed(conn, stage_run_id=stage_run_id, error=failure_text)
            raise SceneFusionBridgeError(f"fusion_child_dispatch_failed:{failure_text[:1200]}")

        async with pool.acquire() as conn:
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set state='running',metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                where attempt_id=$1
                """,
                attempt_id,
                json.dumps(
                    {
                        "children": dispatched,
                        "dispatch_outcome": "accepted",
                        "dispatch_performance": perf,
                        "parent_pricing": reserved_pricing,
                        "execution_mode": "parallel",
                    }
                ),
            )
        return context, attempt_id, attempt_no, attempt_kind, dispatched, reserved_pricing

    async def sync(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        result = await super().sync(
            pool,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select created_at,metadata_json
                from public.v3_studio_stage_attempts
                where stage_run_id=$1
                order by attempt_no desc
                limit 1
                """,
                stage_run_id,
            )
        metadata = _as_dict(row["metadata_json"]) if row else {}
        created_at = row["created_at"] if row else None
        progress = _progress_from_result(
            result=result,
            metadata=metadata,
            created_at=created_at,
        )
        return {**result, "progress": progress}


__all__ = ["ParallelOrphanReconciledParentPricedSceneFusionExecutionService"]
