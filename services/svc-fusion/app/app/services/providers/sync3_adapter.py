from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import httpx

from app.services.providers.base import (
    ProviderClient,
    ProviderPollResult,
    ProviderPrepareInput,
    ProviderPrepareResult,
    ProviderSubmitResult,
)


class Sync3AdapterError(RuntimeError):
    pass


def _sync3_provider_concurrency() -> int:
    try:
        return max(1, min(8, int(os.getenv("DF_SYNC3_PROVIDER_CONCURRENCY") or "1")))
    except Exception:
        return 1


_SYNC3_ACTIVE_GENERATION_SEMAPHORE = asyncio.Semaphore(_sync3_provider_concurrency())


class Sync3Adapter(ProviderClient):
    """Sync Labs sync-3 adapter for deterministic multi-face still-image lipsync."""

    provider_name = "sync3"
    provider_version = "sync.v2"

    def __init__(self) -> None:
        self.base_url = str(os.getenv("SYNC_API_BASE_URL") or "https://api.sync.so").rstrip("/")
        self.api_key = str(os.getenv("SYNC_API_KEY") or "").strip()
        self.model = str(os.getenv("DF_SYNC3_MODEL_ID") or "sync-3").strip() or "sync-3"
        self.timeout_seconds = max(10.0, float(os.getenv("DF_SYNC3_HTTP_TIMEOUT_SECONDS") or "45"))
        self.concurrency_wait_seconds = max(
            30.0,
            float(os.getenv("DF_SYNC3_CONCURRENCY_WAIT_SECONDS") or "900"),
        )
        self._generation_slot_held = False

    @staticmethod
    def _safe_str(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _dict(value: Any) -> Dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _coordinates(payload: Dict[str, Any]) -> list[int]:
        provider_options = Sync3Adapter._dict(payload.get("provider_options"))
        raw = provider_options.get("active_speaker_coordinates")
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise Sync3AdapterError("SYNC3_ACTIVE_SPEAKER_COORDINATES_REQUIRED")
        try:
            x, y = int(raw[0]), int(raw[1])
        except Exception as exc:
            raise Sync3AdapterError("SYNC3_ACTIVE_SPEAKER_COORDINATES_INVALID") from exc
        if x < 0 or y < 0:
            raise Sync3AdapterError("SYNC3_ACTIVE_SPEAKER_COORDINATES_INVALID")
        return [x, y]

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise Sync3AdapterError("SYNC_API_KEY_MISSING")
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _acquire_generation_slot(self) -> None:
        if self._generation_slot_held:
            return
        await _SYNC3_ACTIVE_GENERATION_SEMAPHORE.acquire()
        self._generation_slot_held = True

    def _release_generation_slot(self) -> None:
        if not self._generation_slot_held:
            return
        self._generation_slot_held = False
        _SYNC3_ACTIVE_GENERATION_SEMAPHORE.release()

    @staticmethod
    def _concurrency_retry_seconds(response: httpx.Response) -> Optional[float]:
        if response.status_code != 429:
            return None
        try:
            payload = response.json()
        except Exception:
            return None
        if str(payload.get("errorCode") or "").strip().lower() != "concurrency_limit_reached":
            return None
        try:
            return max(1.0, min(60.0, float(payload.get("retryAfterSeconds") or 20)))
        except Exception:
            return 20.0

    async def prepare(self, data: ProviderPrepareInput) -> ProviderPrepareResult:
        payload = self._dict(getattr(data, "request_payload", None))
        image_url = self._safe_str(getattr(data, "resolved_face_url", None))
        audio_url = self._safe_str(getattr(data, "resolved_audio_url", None))
        if not image_url.startswith(("http://", "https://")):
            raise Sync3AdapterError("SYNC3_IMAGE_URL_REQUIRED")
        if not audio_url.startswith(("http://", "https://")):
            raise Sync3AdapterError("SYNC3_AUDIO_URL_REQUIRED")

        coordinates = self._coordinates(payload)
        request_json = {
            "model": self.model,
            "input": [
                {"type": "image", "url": image_url},
                {"type": "audio", "url": audio_url},
            ],
            "options": {
                "active_speaker_detection": {
                    "auto_detect": False,
                    "frame_number": 0,
                    "coordinates": coordinates,
                }
            },
        }
        return ProviderPrepareResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            request_json=request_json,
            submit_meta={
                "provider_name": self.provider_name,
                "provider_model_name": self.model,
                "input_contract": "multi_face_image+audio+active_speaker_coordinates",
                "active_speaker_coordinates": coordinates,
            },
        )

    async def submit(self, request_json: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        headers = self._headers()
        body = dict(request_json or {})
        if idempotency_key:
            safe_name = "".join(ch for ch in str(idempotency_key) if ch.isalnum() or ch in {"_", "-"})[:120]
            if safe_name:
                body.setdefault("outputFileName", safe_name)

        await self._acquire_generation_slot()
        started = asyncio.get_running_loop().time()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                while True:
                    try:
                        response = await client.post(
                            f"{self.base_url}/v2/generate",
                            headers=headers,
                            json=body,
                        )
                    except Exception as exc:
                        self._release_generation_slot()
                        raise Sync3AdapterError(f"SYNC3_SUBMIT_FAILED:{exc}") from exc

                    retry_after = self._concurrency_retry_seconds(response)
                    if retry_after is not None:
                        elapsed = asyncio.get_running_loop().time() - started
                        if elapsed + retry_after > self.concurrency_wait_seconds:
                            self._release_generation_slot()
                            raise Sync3AdapterError(
                                "SYNC3_CONCURRENCY_WAIT_TIMEOUT:"
                                f"{response.status_code}:{response.text[:1200]}"
                            )
                        await asyncio.sleep(retry_after)
                        continue

                    if response.status_code not in {200, 201, 202}:
                        self._release_generation_slot()
                        raise Sync3AdapterError(
                            f"SYNC3_SUBMIT_FAILED:{response.status_code}:{response.text[:1200]}"
                        )

                    data = response.json()
                    job_id = self._safe_str(data.get("id"))
                    if not job_id:
                        self._release_generation_slot()
                        raise Sync3AdapterError("SYNC3_MISSING_GENERATION_ID")
                    return ProviderSubmitResult(provider_job_id=job_id, raw_response=data)
        except BaseException:
            if not self._generation_slot_held:
                raise
            # Accepted submissions deliberately retain the slot through poll().
            # Any exception escaping before a provider job is returned must release it.
            self._release_generation_slot()
            raise

    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        job_id = self._safe_str(provider_job_id)
        if not job_id:
            raise Sync3AdapterError("SYNC3_GENERATION_ID_REQUIRED")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    f"{self.base_url}/v2/generate/{job_id}",
                    headers=self._headers(),
                )
        except Exception as exc:
            self._release_generation_slot()
            raise Sync3AdapterError(f"SYNC3_STATUS_FAILED:{exc}") from exc
        if response.status_code != 200:
            self._release_generation_slot()
            raise Sync3AdapterError(
                f"SYNC3_STATUS_FAILED:{response.status_code}:{response.text[:1200]}"
            )

        data = response.json()
        status = self._safe_str(data.get("status")).upper()
        if status in {"PENDING", "QUEUED"}:
            return ProviderPollResult(status="queued", video_url=None, share_url=None, error_message=None)
        if status in {"PROCESSING", "RUNNING"}:
            return ProviderPollResult(status="processing", video_url=None, share_url=None, error_message=None)
        if status == "COMPLETED":
            self._release_generation_slot()
            video_url = self._safe_str(data.get("outputUrl") or data.get("segmentOutputUrl"))
            if not video_url:
                raise Sync3AdapterError("SYNC3_COMPLETED_WITHOUT_OUTPUT_URL")
            return ProviderPollResult(status="succeeded", video_url=video_url, share_url=None, error_message=None)
        if status in {"FAILED", "REJECTED"}:
            self._release_generation_slot()
            error = self._safe_str(data.get("error")) or self._safe_str(data.get("errorCode")) or f"SYNC3_{status}"
            return ProviderPollResult(status="failed", video_url=None, share_url=None, error_message=error)
        return ProviderPollResult(status="processing", video_url=None, share_url=None, error_message=None)
