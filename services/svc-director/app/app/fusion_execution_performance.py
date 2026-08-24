from __future__ import annotations

import asyncio
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
    load_fusion_scene_context,
)
from .fusion_execution_resilient import (
    ResilientSceneFusionExecutionService,
    _completed_children,
)


class PooledFusionStudioClient(_base.FusionStudioClient):
    """Connection-pooled svc-fusion client optimized for multi-person scenes.

    Story scenes can contain many dialogue turns. Recreating an HTTP client for every
    quote/status call adds connection setup and TLS overhead and makes polling scale
    linearly with cast/script size. A single bounded connection pool keeps the owner
    service contract unchanged while reducing latency and resource pressure.
    """

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
        # Director needs only state + primary video URL while polling. The light
        # endpoint avoids reloading step history, artifacts and minting SAS URLs for
        # every dialogue child on every poll.
        response = await self._client.get(f"/jobs/{job_id}/status-light", headers=headers)
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_status_failed:{response.status_code}:{response.text[:1200]}"
            )
        payload = dict(response.json() or {})
        if not _clean(payload.get("video_url")):
            payload["video_url"] = _clean(payload.get("primary_video_url") or payload.get("share_url")) or None
        return payload


class PerformantResilientSceneFusionExecutionService(ResilientSceneFusionExecutionService):
    """Resilient scene execution with bounded parallel pricing preview.

    Pricing authority remains svc-pricing via svc-fusion. Director simply issues the
    independent child preview requests concurrently with a conservative limit and
    returns the same quote/fingerprint bundle expected by the existing confirmation
    contract. No price is calculated or overridden in Director.
    """

    pricing_concurrency = 6

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


__all__ = [
    "PerformantResilientSceneFusionExecutionService",
    "PooledFusionStudioClient",
]
