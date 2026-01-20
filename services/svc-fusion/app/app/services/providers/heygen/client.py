from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings
from app.services.providers.base import ProviderSubmitResult, ProviderPollResult

logger = logging.getLogger("heygen_av4")


class HeyGenApiError(RuntimeError):
    pass


def _headers() -> Dict[str, str]:
    if not settings.HEYGEN_API_KEY:
        raise HeyGenApiError("HEYGEN_API_KEY is not set.")
    return {
        "X-Api-Key": settings.HEYGEN_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _safe_json(resp: httpx.Response) -> Dict[str, Any]:
    """
    HeyGen sometimes returns HTTP 200 with an empty body.
    That must be treated as retryable.
    """
    text = (resp.text or "").strip()
    if not text:
        raise HeyGenApiError("HTTP 200 but EMPTY_BODY")
    try:
        obj = resp.json()
    except json.JSONDecodeError as e:
        raise HeyGenApiError(f"INVALID_JSON: {str(e)} body={text[:200]}") from e
    if not isinstance(obj, dict):
        raise HeyGenApiError(f"UNEXPECTED_JSON_TYPE: {type(obj)}")
    return obj


def _normalize_status(raw_status: Any) -> str:
    s = str(raw_status or "").strip().lower()
    if s in ("completed", "complete", "done", "succeeded", "success"):
        return "succeeded"
    if s in ("failed", "error"):
        return "failed"
    if s in ("waiting", "pending", "processing", "running", "in_progress", "in-progress", "queued"):
        return "processing"
    return "processing"


def _extract_video_url(obj: Dict[str, Any]) -> Optional[str]:
    return (
        obj.get("video_url")
        or obj.get("url")
        or (obj.get("result") or {}).get("video_url")
        or (obj.get("data") or {}).get("video_url")
        or (obj.get("data") or {}).get("url")
    )


def _extract_error_message(obj: Dict[str, Any]) -> Optional[str]:
    return (
        obj.get("error_message")
        or obj.get("error")
        or (obj.get("data") or {}).get("error_message")
        or (obj.get("data") or {}).get("error")
        or (obj.get("result") or {}).get("error_message")
        or (obj.get("result") or {}).get("error")
    )


class HeyGenAV4Client:
    provider_name = "heygen_av4"

    def __init__(self) -> None:
        self.base = settings.HEYGEN_BASE_URL.rstrip("/")
        self.timeout = settings.HEYGEN_TIMEOUT_SECONDS

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.6, min=0.6, max=6.0),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError, HeyGenApiError)),
    )
    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        url = f"{self.base}/v2/video/av4/generate"
        headers = _headers()
        headers["Idempotency-Key"] = idempotency_key

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, headers=headers, json=payload)

        if r.status_code >= 400:
            raise HeyGenApiError(f"HeyGen submit failed {r.status_code}: {r.text}")

        data = _safe_json(r)

        provider_job_id = (
            (data.get("data") or {}).get("video_id")
            or data.get("video_id")
            or (data.get("data") or {}).get("id")
            or data.get("id")
        )
        if not provider_job_id:
            raise HeyGenApiError(f"HeyGen submit missing video_id. Response: {data}")

        return ProviderSubmitResult(provider_job_id=str(provider_job_id), raw_response=data)

    @retry(
        reraise=True,
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=0.6, min=0.6, max=8.0),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError, HeyGenApiError)),
    )
    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        res = await self._poll_via_status(provider_job_id)
        if res is not None:
            return res
        return await self._poll_via_list(provider_job_id)

    async def _poll_via_status(self, provider_job_id: str) -> Optional[ProviderPollResult]:
        url = f"{self.base}/v1/video_status.get"
        headers = _headers()
        params = {"video_id": provider_job_id}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, headers=headers, params=params)

        if r.status_code in (404, 405):
            logger.info("heygen_status_endpoint_unavailable", extra={"status_code": r.status_code})
            return None

        if r.status_code >= 400:
            raise HeyGenApiError(f"HeyGen video_status.get failed {r.status_code}: {r.text}")

        data = _safe_json(r)

        core = data.get("data") if isinstance(data.get("data"), dict) else data
        if isinstance(core, dict) and isinstance(core.get("data"), dict):
            core = core["data"]

        if not isinstance(core, dict):
            return ProviderPollResult(status="processing", raw_response={"note": "unexpected status shape", "response": data})

        raw_status = core.get("status") or core.get("state") or core.get("video_status")
        status = _normalize_status(raw_status)

        if status == "succeeded":
            video_url = _extract_video_url(core) or _extract_video_url(data)
            return ProviderPollResult(status="succeeded", video_url=video_url, raw_response=core)

        if status == "failed":
            msg = _extract_error_message(core) or _extract_error_message(data) or "provider failed"
            return ProviderPollResult(status="failed", error_message=str(msg), raw_response=core)

        return ProviderPollResult(status="processing", raw_response=core)

    async def _poll_via_list(self, provider_job_id: str) -> ProviderPollResult:
        url = f"{self.base}/v1/video.list"
        headers = _headers()
        params = {"limit": 50}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, headers=headers, params=params)

        if r.status_code >= 400:
            raise HeyGenApiError(f"HeyGen video.list failed {r.status_code}: {r.text}")

        data = _safe_json(r)

        container = data.get("data")
        videos = None
        if isinstance(container, dict):
            videos = container.get("videos") or container.get("list") or container.get("items")
        elif isinstance(container, list):
            videos = container
        if videos is None:
            videos = data.get("videos") or data.get("list") or data.get("items")

        if not isinstance(videos, list):
            return ProviderPollResult(status="processing", raw_response={"note": "unexpected video.list shape", "response": data})

        item = None
        for v in videos:
            if isinstance(v, dict):
                vid = v.get("video_id") or v.get("id")
                if str(vid) == str(provider_job_id):
                    item = v
                    break

        if item is None:
            return ProviderPollResult(status="processing", raw_response={"note": "video_id not found in list", "video_id": provider_job_id})

        status = _normalize_status(item.get("status") or item.get("state"))
        video_url = _extract_video_url(item)

        if status == "succeeded":
            return ProviderPollResult(status="succeeded", video_url=video_url, raw_response=item)

        if status == "failed":
            msg = _extract_error_message(item) or "provider failed"
            return ProviderPollResult(status="failed", error_message=str(msg), raw_response=item)

        return ProviderPollResult(status="processing", raw_response=item)

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.6, min=0.6, max=6.0),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError, HeyGenApiError)),
    )
    async def get_share_url(self, provider_job_id: str) -> dict:
        url = f"{self.base}/v1/video/share"
        headers = _headers()
        payload = {"video_id": provider_job_id}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, headers=headers, json=payload)

        if r.status_code >= 400:
            raise HeyGenApiError(f"HeyGen video.share failed {r.status_code}: {r.text}")

        data = _safe_json(r)

        share_url = (
            (data.get("data") or {}).get("url")
            or (data.get("data") or {}).get("share_url")
            or data.get("url")
            or data.get("share_url")
        )
        return {"share_url": share_url, "raw": data}