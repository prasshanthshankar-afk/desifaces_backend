from __future__ import annotations

import asyncio
import httpx
from typing import Any, Optional

from app.config import settings


class FusionExtensionService:
    """
    Client for svc-fusion-extension music composition.

    Supports both:
      - sync response: { preview_url, outputs:{...} } (or wrapped in {data:{...}})
      - async response: { status:"queued", compose_job_id:"..." } and we poll:
            GET /api/fusion-extension/compose_music/{compose_job_id}
    """

    def __init__(self):
        self.base_url = settings.FUSION_EXTENSION_URL.rstrip("/")
        self.internal_key = settings.INTERNAL_API_KEY

        # Reasonable timeouts: long overall, short connect/read headers.
        self.timeout = httpx.Timeout(timeout=300.0, connect=10.0)

    def _headers(self, request_id: Optional[str] = None) -> dict[str, str]:
        h = {
            "X-Internal-Key": self.internal_key,
            "Content-Type": "application/json",
        }
        # Helps trace requests across services; also works as idempotency key if you implement it server-side
        if request_id:
            h["X-Request-Id"] = request_id
            h["Idempotency-Key"] = request_id
        return h

    async def _post_json_with_retries(
        self,
        *,
        url: str,
        payload: dict,
        headers: dict[str, str],
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        backoffs = [0.5, 1.0, 2.0, 4.0]  # seconds
        last_err: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    r = await client.post(url, headers=headers, json=payload)

                # Retry on rate limit / transient server errors
                if r.status_code in (429, 500, 502, 503, 504):
                    # Try to read json for debugging; ignore if invalid
                    try:
                        j = r.json()
                    except Exception:
                        j = {"text": r.text[:500]}
                    last_err = RuntimeError(f"fusion_extension_http_{r.status_code}: {j}")
                    if attempt < max_attempts:
                        await asyncio.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)])
                        continue
                    raise last_err

                r.raise_for_status()
                return r.json()

            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                if attempt < max_attempts:
                    await asyncio.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)])
                    continue
                raise RuntimeError(f"fusion_extension_network_error: {e}") from e

        raise RuntimeError(f"fusion_extension_unknown_error: {last_err}")

    async def _get_json_with_retries(
        self,
        *,
        url: str,
        headers: dict[str, str],
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        backoffs = [0.5, 1.0, 2.0, 4.0]
        last_err: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    r = await client.get(url, headers=headers)

                if r.status_code in (429, 500, 502, 503, 504):
                    try:
                        j = r.json()
                    except Exception:
                        j = {"text": r.text[:500]}
                    last_err = RuntimeError(f"fusion_extension_http_{r.status_code}: {j}")
                    if attempt < max_attempts:
                        await asyncio.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)])
                        continue
                    raise last_err

                r.raise_for_status()
                return r.json()

            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                if attempt < max_attempts:
                    await asyncio.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)])
                    continue
                raise RuntimeError(f"fusion_extension_network_error: {e}") from e

        raise RuntimeError(f"fusion_extension_unknown_error: {last_err}")

    def _unwrap_data(self, resp: dict[str, Any]) -> dict[str, Any]:
        # Support either {data:{...}} or flat {...}
        if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
            return resp["data"]
        return resp

    async def poll_compose_job(
        self,
        *,
        compose_job_id: str,
        request_id: Optional[str] = None,
        poll_interval_s: float = 3.0,
        timeout_s: int = 900,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/fusion-extension/compose_music/{compose_job_id}"
        headers = self._headers(request_id=request_id)

        waited = 0.0
        while waited < timeout_s:
            resp = await self._get_json_with_retries(url=url, headers=headers)
            data = self._unwrap_data(resp)
            status = (data.get("status") or "").lower()

            if status in ("succeeded", "failed"):
                return resp

            await asyncio.sleep(poll_interval_s)
            waited += poll_interval_s

        raise RuntimeError("fusion_extension_compose_timeout")

    async def compose_music(self, payload: dict) -> dict:
        """
        Returns a response dict (either sync output or final polled async output).
        """
        url = f"{self.base_url}/api/fusion-extension/compose_music"
        request_id = payload.get("job_id") or payload.get("project_id")
        headers = self._headers(request_id=str(request_id) if request_id else None)

        resp = await self._post_json_with_retries(url=url, payload=payload, headers=headers)
        data = self._unwrap_data(resp)

        # If fusion-extension returns async job, poll it.
        # Example async: {"status":"queued","compose_job_id":"..."}
        status = (data.get("status") or "").lower()
        compose_job_id = data.get("compose_job_id") or data.get("id")

        if status in ("queued", "running") and compose_job_id:
            return await self.poll_compose_job(compose_job_id=str(compose_job_id), request_id=str(request_id))

        return resp