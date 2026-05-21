
from __future__ import annotations

import os
from typing import Any, Dict

from app.services.providers.base import (
    ProviderClient,
    ProviderPollResult,
    ProviderPrepareInput,
    ProviderPrepareResult,
    ProviderSubmitResult,
)
from app.services.providers.fal_queue import FalQueueClient, FalQueueError


class RunwayAdapterError(RuntimeError):
    pass


class RunwayAdapter(ProviderClient):
    provider_name = "runway"
    provider_version = "fal.v1"

    def __init__(self) -> None:
        self.client = FalQueueClient()
        self.model_id = (os.getenv("FAL_RUNWAY_MODEL_ID") or "").strip()
        if not self.model_id:
            raise RunwayAdapterError("FAL_RUNWAY_MODEL_ID not configured")

    async def prepare(self, data: ProviderPrepareInput) -> ProviderPrepareResult:
        payload = dict(data.request_payload or {})
        provider_options = payload.get("provider_options") if isinstance(payload.get("provider_options"), dict) else {}
        input_payload = dict(provider_options.get("input") or {})
        # Fall back to a generic image/text/video prompt shape if caller did not provide explicit input.
        input_payload.setdefault("prompt", str(provider_options.get("prompt") or payload.get("prompt") or "").strip())
        if data.resolved_face_url and "image_url" not in input_payload and "start_image_url" not in input_payload:
            input_payload["image_url"] = data.resolved_face_url
        if data.reference_image_urls and "reference_image_urls" not in input_payload:
            input_payload["reference_image_urls"] = list(data.reference_image_urls)

        if not any(input_payload.get(k) for k in ("prompt", "image_url", "start_image_url", "video_url", "reference_image_urls")):
            raise RunwayAdapterError("runway prepare requires provider_options.input or basic prompt/image inputs")

        return ProviderPrepareResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            request_json={"model_id": self.model_id, "input": input_payload},
            submit_meta={"provider_name": self.provider_name, "provider_model_name": self.model_id},
        )

    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        try:
            resp = await self.client.submit(str(payload["model_id"]), dict(payload.get("input") or {}), idempotency_key=idempotency_key)
        except FalQueueError as e:
            raise RunwayAdapterError(str(e)) from e
        request_id = str(resp.get("request_id") or "").strip()
        if not request_id:
            raise RunwayAdapterError("runway submit missing request_id")
        return ProviderSubmitResult(provider_job_id=request_id, raw_response=resp)

    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        try:
            st = await self.client.status(self.model_id, provider_job_id, logs=True)
            normalized, error_message = self.client.normalize_status(st)
            if normalized != "succeeded":
                return ProviderPollResult(status=normalized, raw_response=st, error_message=error_message)
            result = await self.client.result(self.model_id, provider_job_id)
        except FalQueueError as e:
            raise RunwayAdapterError(str(e)) from e
        video_url = self.client.extract_video_url(result)
        if not video_url:
            return ProviderPollResult(status="failed", raw_response=result, error_message="runway result missing video url")
        return ProviderPollResult(status="succeeded", video_url=video_url, raw_response=result)
