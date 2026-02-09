from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.config import settings

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


@dataclass(frozen=True)
class FalQueueSubmitResult:
    request_id: str
    response_url: str
    status_url: str
    cancel_url: str


class FalQueueClient:
    """
    Minimal wrapper around fal Queue endpoints.

    NOTE:
      - queue.fal.run expects the model payload as TOP-LEVEL JSON (NOT wrapped in {"input": {...}}).
    """

    def __init__(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = (
            (api_key or "").strip()
            or (getattr(settings, "FAL_KEY", None) or "").strip()
            or (getattr(settings, "FAL_API_KEY", None) or "").strip()
        )
        if not self.api_key:
            raise RuntimeError("missing_fal_key")

        self.base_url = (base_url or getattr(settings, "FAL_QUEUE_BASE_URL", None) or "https://queue.fal.run").strip()
        if httpx is None:
            raise RuntimeError("missing_dependency_httpx")

    def _auth_headers(self) -> Dict[str, str]:
        # fal Queue auth uses Authorization: Key <FAL_KEY>
        return {"Authorization": f"Key {self.api_key}"}

    async def submit(
        self,
        *,
        model_id: str,
        payload: Dict[str, Any],
        object_lifecycle_seconds: Optional[int] = None,
        start_timeout_seconds: Optional[int] = None,
    ) -> FalQueueSubmitResult:
        model_id = (model_id or "").strip().lstrip("/")
        if not model_id:
            raise ValueError("model_id_required")

        url = f"{self.base_url.rstrip('/')}/{model_id}"

        headers = dict(self._auth_headers())
        headers["Content-Type"] = "application/json"

        if object_lifecycle_seconds and int(object_lifecycle_seconds) > 0:
            headers["X-Fal-Object-Lifecycle-Preference"] = json.dumps(
                {"expiration_duration_seconds": int(object_lifecycle_seconds)}
            )
        if start_timeout_seconds and int(start_timeout_seconds) > 0:
            headers["X-Fal-Request-Timeout"] = str(int(start_timeout_seconds))

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:  # type: ignore
            # IMPORTANT: payload fields must be top-level (no {"input": {...}} wrapper)
            r = await client.post(url, headers=headers, json=(payload or {}))
            r.raise_for_status()
            j = r.json()

        return FalQueueSubmitResult(
            request_id=str(j.get("request_id") or ""),
            response_url=str(j.get("response_url") or ""),
            status_url=str(j.get("status_url") or ""),
            cancel_url=str(j.get("cancel_url") or ""),
        )

    async def status(self, *, status_url: str) -> Dict[str, Any]:
        status_url = (status_url or "").strip()
        if not status_url:
            raise ValueError("status_url_required")

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:  # type: ignore
            r = await client.get(status_url, headers=self._auth_headers())
            # status endpoint can return 202 while queued/in-progress
            if r.status_code >= 400 and r.status_code != 202:
                r.raise_for_status()
            return r.json()

    async def result(self, *, response_url: str) -> Dict[str, Any]:
        response_url = (response_url or "").strip()
        if not response_url:
            raise ValueError("response_url_required")

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:  # type: ignore
            r = await client.get(response_url, headers=self._auth_headers())
            r.raise_for_status()
            return r.json()

    async def wait_for_completion(
        self,
        *,
        status_url: str,
        response_url: str,
        poll_seconds: float,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        start = time.monotonic()
        poll = max(0.5, float(poll_seconds or 2.5))
        timeout = max(10, int(timeout_seconds or 900))

        while True:
            if (time.monotonic() - start) > timeout:
                raise TimeoutError(f"fal_queue_timeout_after_seconds:{timeout}")

            st = await self.status(status_url=status_url)
            status = str(st.get("status") or "").upper()

            if status == "COMPLETED":
                return await self.result(response_url=response_url)

            if status == "FAILED":
                # keep the last status payload for debugging (logs/trace)
                raise RuntimeError(f"fal_queue_failed:{json.dumps(st, ensure_ascii=False)}")

            # IN_QUEUE / IN_PROGRESS / unknown => keep polling
            await asyncio.sleep(poll)