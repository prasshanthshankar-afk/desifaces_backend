from __future__ import annotations

import asyncio
import os
import time
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


_SYNC3_SUBMISSION_GATE: asyncio.Semaphore | None = None
_SYNC3_SUBMISSION_GATE_LIMIT: int | None = None


def _provider_concurrency_limit() -> int:
    raw = str(os.getenv("DF_SYNC3_PROVIDER_CONCURRENCY") or "1").strip()
    try:
        return max(1, min(16, int(raw)))
    except Exception:
        return 1


def _provider_wait_seconds() -> float:
    raw = str(os.getenv("DF_SYNC3_CONCURRENCY_WAIT_SECONDS") or "900").strip()
    try:
        return max(5.0, min(3600.0, float(raw)))
    except Exception:
        return 900.0


def _submission_gate() -> asyncio.Semaphore:
    global _SYNC3_SUBMISSION_GATE, _SYNC3_SUBMISSION_GATE_LIMIT
    limit = _provider_concurrency_limit()
    if _SYNC3_SUBMISSION_GATE is None or _SYNC3_SUBMISSION_GATE_LIMIT != limit:
        _SYNC3_SUBMISSION_GATE = asyncio.Semaphore(limit)
        _SYNC3_SUBMISSION_GATE_LIMIT = limit
    return _SYNC3_SUBMISSION_GATE


def _active_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("activeGenerations", "active_generations", "count", "total"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return max(0, int(value))
        for key in ("data", "items", "results", "generations"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    raise Sync3AdapterError("SYNC3_ACTIVE_GENERATIONS_RESPONSE_INVALID")


def _concurrency_retry_after(response: httpx.Response) -> float | None:
    """Return a bounded retry delay only for Sync's temporary concurrency 429."""

    if int(response.status_code) != 429:
        return None

    try:
        payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    if str(payload.get("errorCode") or payload.get("error_code") or "").strip().lower() != "concurrency_limit_reached":
        return None

    try:
        retry_after = float(payload.get("retryAfterSeconds") or payload.get("retry_after_seconds") or 5.0)
    except Exception:
        retry_after = 5.0
    return max(1.0, min(20.0, retry_after))


class Sync3Adapter(ProviderClient):
    """Sync Labs sync-3 adapter for deterministic multi-face still-image lipsync."""

    provider_name = "sync3"
    provider_version = "sync.v2"

    def __init__(self) -> None:
        self.base_url = str(os.getenv("SYNC_API_BASE_URL") or "https://api.sync.so").rstrip("/")
        self.api_key = str(os.getenv("SYNC_API_KEY") or "").strip()
        self.model = str(os.getenv("DF_SYNC3_MODEL_ID") or "sync-3").strip() or "sync-3"
        self.timeout_seconds = max(10.0, float(os.getenv("DF_SYNC3_HTTP_TIMEOUT_SECONDS") or "45"))
        self.provider_concurrency = _provider_concurrency_limit()
        self.concurrency_wait_seconds = _provider_wait_seconds()

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

    async def _wait_for_submission_capacity(
        self,
        client: httpx.AsyncClient,
        headers: Dict[str, str],
        *,
        deadline: float | None = None,
    ) -> None:
        """Wait until Sync reports capacity before a new generation is submitted.

        The module-level semaphore serializes capacity-check + submit so two local
        Fusion jobs cannot both observe the same free provider slot and race.
        Provider-side active generation count remains authoritative, which also
        protects us after worker restarts or generations started elsewhere.
        """
        if deadline is None:
            deadline = time.monotonic() + float(self.concurrency_wait_seconds)
        last_active: int | None = None
        last_status = 0
        last_text = ""

        while True:
            try:
                response = await client.get(
                    f"{self.base_url}/v2/generations?status=PROCESSING",
                    headers=headers,
                )
                last_status = int(response.status_code)
                last_text = response.text[:1200]
                if response.status_code != 200:
                    raise Sync3AdapterError(
                        f"SYNC3_ACTIVE_GENERATIONS_FAILED:{response.status_code}:{last_text}"
                    )
                payload = response.json()
                last_active = _active_count(payload)
            except Sync3AdapterError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(5.0)
                continue
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise Sync3AdapterError(
                        f"SYNC3_CONCURRENCY_WAIT_TIMEOUT:{last_status}:{last_text or str(exc)[:1200]}"
                    ) from exc
                await asyncio.sleep(5.0)
                continue

            if last_active < int(self.provider_concurrency):
                return

            if time.monotonic() >= deadline:
                raise Sync3AdapterError(
                    "SYNC3_CONCURRENCY_WAIT_TIMEOUT:"
                    f"active={last_active}:limit={self.provider_concurrency}"
                )

            retry_after = 5.0
            if isinstance(payload, dict):
                try:
                    retry_after = float(payload.get("retryAfterSeconds") or payload.get("retry_after_seconds") or 5.0)
                except Exception:
                    retry_after = 5.0
            await asyncio.sleep(max(1.0, min(20.0, retry_after)))

    async def submit(self, request_json: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        headers = self._headers()
        body = dict(request_json or {})
        if idempotency_key:
            safe_name = "".join(ch for ch in str(idempotency_key) if ch.isalnum() or ch in {"_", "-"})[:120]
            if safe_name:
                body.setdefault("outputFileName", safe_name)

        # One bounded window covers both provider-capacity polling and the
        # unavoidable race where capacity changes between GET and POST.
        deadline = time.monotonic() + float(self.concurrency_wait_seconds)
        gate = _submission_gate()
        async with gate:
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    while True:
                        await self._wait_for_submission_capacity(
                            client,
                            headers,
                            deadline=deadline,
                        )
                        response = await client.post(
                            f"{self.base_url}/v2/generate",
                            headers=headers,
                            json=body,
                        )

                        retry_after = _concurrency_retry_after(response)
                        if retry_after is None:
                            break

                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise Sync3AdapterError(
                                "SYNC3_CONCURRENCY_WAIT_TIMEOUT:"
                                f"submit_429:{response.text[:1200]}"
                            )

                        # Keep the same request body/outputFileName so retries
                        # preserve the original provider idempotency identity.
                        await asyncio.sleep(min(retry_after, remaining))
            except Sync3AdapterError:
                raise
            except Exception as exc:
                raise Sync3AdapterError(f"SYNC3_SUBMIT_FAILED:{exc}") from exc

        if response.status_code not in {200, 201, 202}:
            raise Sync3AdapterError(
                f"SYNC3_SUBMIT_FAILED:{response.status_code}:{response.text[:1200]}"
            )
        data = response.json()
        job_id = self._safe_str(data.get("id"))
        if not job_id:
            raise Sync3AdapterError("SYNC3_MISSING_GENERATION_ID")
        return ProviderSubmitResult(provider_job_id=job_id, raw_response=data)

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
            raise Sync3AdapterError(f"SYNC3_STATUS_FAILED:{exc}") from exc
        if response.status_code != 200:
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
            video_url = self._safe_str(data.get("outputUrl") or data.get("segmentOutputUrl"))
            if not video_url:
                raise Sync3AdapterError("SYNC3_COMPLETED_WITHOUT_OUTPUT_URL")
            return ProviderPollResult(status="succeeded", video_url=video_url, share_url=None, error_message=None)
        if status in {"FAILED", "REJECTED"}:
            error = self._safe_str(data.get("error")) or self._safe_str(data.get("errorCode")) or f"SYNC3_{status}"
            return ProviderPollResult(status="failed", video_url=None, share_url=None, error_message=error)
        return ProviderPollResult(status="processing", video_url=None, share_url=None, error_message=None)
