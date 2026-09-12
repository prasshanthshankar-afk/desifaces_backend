from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx

from . import fusion_execution as _base
from .fusion_execution import (
    SceneFusionBridgeError,
    _as_dict,
    _clean,
    _compile_children,
    _latest_attempt,
    _latest_output_review,
    _video_url_from_status,
    load_fusion_scene_context,
)
from .fusion_execution_resilient import (
    ResilientSceneFusionExecutionService,
    _completed_children,
)


class PooledFusionStudioClient(_base.FusionStudioClient):
    """Connection-pooled svc-fusion client optimized for multi-person scenes."""

    def __init__(self, *, base_url: str, timeout_seconds: float = 45.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        )

    async def preview_pricing(self, *, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/jobs/pricing/preview", headers=headers, json=payload)
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_pricing_preview_failed:{response.status_code}:{response.text[:1200]}"
            )
        return response.json()

    async def create_job(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        quote_id: str,
        preview_fingerprint: str | None,
    ) -> str:
        body = dict(payload)
        body["pricing_confirmation"] = {
            "quote_id": quote_id,
            "preview_fingerprint": preview_fingerprint,
            "user_confirmed": True,
        }
        response = await self._client.post("/jobs", headers=headers, json=body)
        if response.status_code not in {200, 201, 202}:
            raise SceneFusionBridgeError(
                f"fusion_generate_failed:{response.status_code}:{response.text[:1200]}"
            )
        job_id = _clean(response.json().get("job_id") or response.json().get("id"))
        if not job_id:
            raise SceneFusionBridgeError(f"fusion_generate_missing_job_id:{response.text[:1200]}")
        return job_id

    async def status(self, *, headers: dict[str, str], job_id: str) -> dict[str, Any]:
        # Poll the compact endpoint. It avoids loading step history, artifact rows and
        # minting SAS URLs for every dialogue child on every Studio refresh.
        response = await self._client.get(f"/jobs/{job_id}/status-light", headers=headers)
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_status_failed:{response.status_code}:{response.text[:1200]}"
            )
        payload = dict(response.json() or {})
        if not _clean(payload.get("video_url")):
            payload["video_url"] = _clean(payload.get("primary_video_url") or payload.get("share_url")) or None
        return payload

    async def status_full(self, *, headers: dict[str, str], job_id: str) -> dict[str, Any]:
        # Terminal-success fallback only. Some older provider paths populate the
        # artifact table before light_status.primary_video_url. Heavy status is never
        # used for routine polling.
        response = await self._client.get(f"/jobs/{job_id}/status", headers=headers)
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_status_full_failed:{response.status_code}:{response.text[:1200]}"
            )
        return dict(response.json() or {})


class PerformantResilientSceneFusionExecutionService(ResilientSceneFusionExecutionService):
    """Resilient scene execution with bounded parallel pricing and status polling.

    svc-pricing remains authoritative through svc-fusion. Director never computes or
    overrides price. Successful children from failed attempts are preserved. Routine
    polling uses the light Fusion status endpoint and bounded concurrency; a heavy
    status lookup is used only as a terminal-success fallback when no video URL has
    propagated into the light view yet.
    """

    pricing_concurrency = 6
    status_concurrency = 8

    async def preview(
        self,
        conn,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        external_provider_ok: bool,
    ):
        context = await load_fusion_scene_context(
            conn,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
        )
        if context.stage_state not in {"pending", "ready", "failed", "rejected"}:
            raise SceneFusionBridgeError(f"fusion_stage_not_priceable:{context.stage_state}")
        await self.store.assert_startable(conn, stage_run_id=stage_run_id)

        preserved: dict[str, dict[str, Any]] = {}
        if context.stage_state == "failed":
            latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
            if latest:
                preserved = _completed_children(_as_dict(latest["metadata_json"]))

        required_turn_ids = {
            str(turn.dialogue_turn_id)
            for turn in context.turns
            if str(turn.dialogue_turn_id) not in preserved
        }
        if not required_turn_ids:
            return context, []

        request_nonce_by_turn = {turn_id: uuid4().hex for turn_id in required_turn_ids}
        children = await _compile_children(
            context=context,
            face_client=self.face_client,
            audio_client=self.audio_client,
            headers=headers,
            external_provider_ok=external_provider_ok,
            request_nonce_by_turn=request_nonce_by_turn,
        )
        required_children = [
            child for child in children if child["dialogue_turn_id"] in required_turn_ids
        ]
        semaphore = asyncio.Semaphore(
            max(1, min(int(self.pricing_concurrency), len(required_children)))
        )

        async def price_child(child: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                preview = await self.fusion_client.preview_pricing(
                    headers=headers,
                    payload=child["payload"],
                )
            pricing = _as_dict(preview.get("pricing"))
            turn_id = child["dialogue_turn_id"]
            quote_id = _clean(preview.get("quote_id") or pricing.get("quote_id"))
            if not quote_id:
                raise SceneFusionBridgeError(
                    f"fusion_pricing_preview_missing_quote_id:{turn_id}"
                )
            return {
                "dialogue_turn_id": turn_id,
                "participant_id": child["participant_id"],
                "display_name": child["display_name"],
                "sequence_no": child["sequence_no"],
                "request_nonce": request_nonce_by_turn[turn_id],
                "quote_id": quote_id,
                "preview_fingerprint": preview.get("preview_fingerprint")
                or pricing.get("preview_fingerprint"),
                "pricing": pricing,
                "pricing_summary": preview.get("pricing_summary") or {},
                "message": preview.get("message"),
                "retry_scope": "failed_child_only" if preserved else "initial_scene",
                "preserved_child_count": len(preserved),
            }

        quotes = await asyncio.gather(*(price_child(child) for child in required_children))
        quotes.sort(key=lambda item: int(item.get("sequence_no") or 0))
        return context, quotes

    async def sync(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        async with pool.acquire() as conn:
            context = await load_fusion_scene_context(
                conn,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
            )
            existing = await _latest_output_review(conn, stage_run_id=stage_run_id)
            latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
            if existing and context.stage_state in {"awaiting_review", "approved", "rejected"}:
                media_id = UUID(str(existing["media_id"]))
                video_url = await self.stitch_client.read_url(headers=headers, media_id=media_id)
                return {
                    "workflow_id": str(workflow_id),
                    "stage_run_id": str(stage_run_id),
                    "scene_id": str(context.scene_id),
                    "provider_state": "succeeded",
                    "stage_state": context.stage_state,
                    "media_asset_id": str(media_id),
                    "video_url": video_url,
                    "review_item_id": str(existing["review_item_id"]) if existing["review_item_id"] else None,
                    "review_decision": str(existing["decision"]) if existing["decision"] else None,
                    "children": list(_as_dict(latest["metadata_json"]).get("children") or []) if latest else [],
                }
            if not latest:
                return {
                    "workflow_id": str(workflow_id),
                    "stage_run_id": str(stage_run_id),
                    "scene_id": str(context.scene_id),
                    "provider_state": None,
                    "stage_state": context.stage_state,
                    "media_asset_id": None,
                    "video_url": None,
                    "review_item_id": None,
                    "review_decision": None,
                    "children": [],
                }
            attempt_id = UUID(str(latest["attempt_id"]))
            metadata = _as_dict(latest["metadata_json"])
            children = list(metadata.get("children") or [])
            if not children:
                raise SceneFusionBridgeError("fusion_attempt_has_no_child_jobs")

        semaphore = asyncio.Semaphore(
            max(1, min(int(self.status_concurrency), len(children)))
        )

        async def refresh_child(raw: dict[str, Any]) -> dict[str, Any]:
            item = dict(raw or {})
            current_state = _clean(item.get("status")).lower()
            current_url = _clean(item.get("video_url"))
            if (
                bool(item.get("reused_from_prior_attempt"))
                and current_state in {"succeeded", "completed", "complete", "ready"}
                and current_url
            ):
                item["status"] = "succeeded"
                return item

            job_id = _clean(item.get("fusion_job_id"))
            if not job_id:
                item["status"] = "failed"
                item["error"] = "missing_fusion_job_id"
                return item

            try:
                async with semaphore:
                    payload = await self.fusion_client.status(headers=headers, job_id=job_id)
            except Exception as exc:
                # Poll transport errors are ambiguous and must not convert a paid,
                # potentially running provider job into a failed logical attempt.
                item["poll_error"] = str(exc)[:1000]
                return item

            state = _clean(payload.get("status")).lower()
            item["status"] = state or current_state or "unknown"
            item["error_code"] = payload.get("error_code")
            item["error_message"] = payload.get("error_message")
            item.pop("poll_error", None)

            if state in {"succeeded", "completed", "complete", "ready"}:
                video_url = _video_url_from_status(payload)
                if not video_url and isinstance(self.fusion_client, PooledFusionStudioClient):
                    try:
                        async with semaphore:
                            full = await self.fusion_client.status_full(headers=headers, job_id=job_id)
                        video_url = _video_url_from_status(full)
                    except Exception as exc:
                        item["terminal_artifact_error"] = str(exc)[:1000]
                if video_url:
                    item["video_url"] = video_url
                    item["status"] = "succeeded"
                else:
                    # Treat missing artifact visibility as recoverable polling state,
                    # not a provider failure. A later poll can observe the artifact.
                    item["status"] = "finalizing"
            return item

        refreshed = await asyncio.gather(*(refresh_child(dict(child or {})) for child in children))
        refreshed.sort(key=lambda item: int(item.get("sequence_no") or 0))

        any_failed = any(
            _clean(item.get("status")).lower() in {"failed", "canceled", "cancelled"}
            for item in refreshed
        )
        all_succeeded = bool(refreshed) and all(
            _clean(item.get("status")).lower() in {"succeeded", "completed", "complete", "ready"}
            and bool(_clean(item.get("video_url")))
            for item in refreshed
        )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                where attempt_id=$1
                """,
                attempt_id,
                json.dumps({"children": refreshed}),
            )

        if any_failed:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='failed',completed_at=coalesce(completed_at,now()),
                        error_code='fusion_child_failed',
                        error_message='one_or_more_child_fusion_jobs_failed',updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                )
                await self.store.mark_failed(
                    conn,
                    stage_run_id=stage_run_id,
                    error="one_or_more_child_fusion_jobs_failed",
                )
            raise SceneFusionBridgeError("fusion_child_job_failed")

        if not all_succeeded:
            return {
                "workflow_id": str(workflow_id),
                "stage_run_id": str(stage_run_id),
                "scene_id": str(context.scene_id),
                "provider_state": "running",
                "stage_state": "generating",
                "media_asset_id": None,
                "video_url": None,
                "review_item_id": None,
                "review_decision": None,
                "children": refreshed,
            }

        ordered_urls = [_clean(item.get("video_url")) for item in refreshed]
        try:
            stitch = await self.stitch_client.stitch(
                headers=headers,
                project_id=context.project_id,
                workflow_id=context.workflow_id,
                stage_run_id=context.stage_run_id,
                attempt_id=attempt_id,
                segment_urls=ordered_urls,
            )
            media_id = UUID(str(stitch.get("media_id")))
            video_url = _clean(stitch.get("video_url"))
            if not video_url:
                raise SceneFusionBridgeError("fusion_scene_stitch_missing_video_url")
        except Exception as exc:
            # Every child is already reusable. Mark only the logical scene attempt
            # failed so the next recovery can be stitch-only and charge-free.
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='failed',completed_at=coalesce(completed_at,now()),
                        error_code='fusion_scene_stitch_failed',error_message=$2,
                        metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,
                        updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    str(exc)[:4000],
                    json.dumps({
                        "children": refreshed,
                        "dispatch_outcome": "children_complete_stitch_failed",
                        "retry_scope": "stitch_only",
                    }),
                )
                await self.store.mark_failed(
                    conn,
                    stage_run_id=stage_run_id,
                    error="fusion_scene_stitch_failed",
                )
            if isinstance(exc, SceneFusionBridgeError):
                raise
            raise SceneFusionBridgeError(f"fusion_scene_stitch_failed:{str(exc)[:1200]}") from exc

        async with pool.acquire() as conn:
            async with conn.transaction():
                media = await conn.fetchrow(
                    "select id,account_id,project_id,user_id from public.media_assets where id=$1",
                    media_id,
                )
                if not media or str(media["account_id"]) != str(account_id):
                    raise SceneFusionBridgeError("fusion_final_media_account_mismatch")
                if media["project_id"] and str(media["project_id"]) != str(context.project_id):
                    raise SceneFusionBridgeError("fusion_final_media_project_mismatch")

                review_id = await self.store.attach_output(
                    conn,
                    stage_run_id=stage_run_id,
                    media_id=media_id,
                    output_role="scene_video_candidate",
                )
                for turn in context.turns:
                    face_stage_id = await conn.fetchval(
                        """
                        select stage_run_id from public.v3_studio_stage_runs
                        where workflow_id=$1 and stage_type='face' and scope_type='participant'
                          and participant_id=$2 and state='approved'
                        order by created_at desc limit 1
                        """,
                        workflow_id,
                        turn.participant_id,
                    )
                    audio_stage_id = await conn.fetchval(
                        """
                        select stage_run_id from public.v3_studio_stage_runs
                        where workflow_id=$1 and stage_type='audio' and scope_type='dialogue_turn'
                          and dialogue_turn_id=$2 and state='approved'
                        order by created_at desc limit 1
                        """,
                        workflow_id,
                        turn.dialogue_turn_id,
                    )
                    if face_stage_id:
                        await self.store.bind_approved_input(
                            conn,
                            stage_run_id=stage_run_id,
                            media_id=turn.face_media_id,
                            input_role="approved_speaker_face",
                            source_stage_run_id=UUID(str(face_stage_id)),
                        )
                    if audio_stage_id:
                        await self.store.bind_approved_input(
                            conn,
                            stage_run_id=stage_run_id,
                            media_id=turn.audio_media_id,
                            input_role="approved_dialogue_audio",
                            source_stage_run_id=UUID(str(audio_stage_id)),
                        )

                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='succeeded',completed_at=coalesce(completed_at,now()),media_id=$2,
                        metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    media_id,
                    json.dumps({"children": refreshed, "stitched_video_url": video_url}),
                )
                await conn.execute(
                    """
                    update public.v3_studio_stage_runs
                    set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where stage_run_id=$1
                    """,
                    stage_run_id,
                    json.dumps({"canonical_scene_video_media_id": str(media_id)}),
                )

        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "scene_id": str(context.scene_id),
            "provider_state": "succeeded",
            "stage_state": "awaiting_review",
            "media_asset_id": str(media_id),
            "video_url": video_url,
            "review_item_id": str(review_id),
            "review_decision": "pending",
            "children": refreshed,
        }


__all__ = [
    "PerformantResilientSceneFusionExecutionService",
    "PooledFusionStudioClient",
]
