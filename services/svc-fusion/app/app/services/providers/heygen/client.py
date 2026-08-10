from __future__ import annotations

import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.services.providers.base import ProviderPollResult, ProviderSubmitResult

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
    Treat that as retryable.
    """
    text = (resp.text or "").strip()
    if not text:
        raise HeyGenApiError("HTTP 200 but EMPTY_BODY")
    try:
        obj = resp.json()
    except json.JSONDecodeError as e:
        raise HeyGenApiError(f"INVALID_JSON: {str(e)} body={text[:400]}") from e
    if not isinstance(obj, dict):
        raise HeyGenApiError(f"UNEXPECTED_JSON_TYPE: {type(obj)}")
    return obj


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, HeyGenApiError):
        msg = str(exc).lower()
        return (
            "empty_body" in msg
            or "invalid_json" in msg
            or "timeout" in msg
            or "transport" in msg
            or "503" in msg
            or "502" in msg
            or "504" in msg
            or "429" in msg
        )
    return False


def _normalize_status(raw_status: Any) -> str:
    s = str(raw_status or "").strip().lower()
    if s in ("completed", "complete", "done", "succeeded", "success", "ready"):
        return "succeeded"
    if s in ("failed", "error"):
        return "failed"
    if s in ("cancelled", "canceled"):
        return "failed"
    if s in (
        "waiting",
        "pending",
        "processing",
        "running",
        "in_progress",
        "in-progress",
        "queued",
        "submitted",
    ):
        return "processing"
    return "processing"


def _extract_video_url(obj: Dict[str, Any]) -> Optional[str]:
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
    return (
        obj.get("video_url")
        or obj.get("url")
        or data.get("video_url")
        or data.get("url")
        or result.get("video_url")
        or result.get("url")
    )


def _extract_error_message(obj: Dict[str, Any]) -> Optional[str]:
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
    err = obj.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("code")
    return (
        obj.get("error_message")
        or err
        or obj.get("message")
        or data.get("error_message")
        or data.get("error")
        or data.get("message")
        or result.get("error_message")
        or result.get("error")
        or result.get("message")
    )


def _extract_provider_job_id(obj: Dict[str, Any]) -> Optional[str]:
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
    candidates = [
        data.get("video_id"),
        obj.get("video_id"),
        data.get("id"),
        obj.get("id"),
        result.get("video_id"),
        result.get("id"),
    ]
    for v in candidates:
        if v:
            return str(v).strip()
    return None


def _unwrap_status_core(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Accept a few HeyGen response shapes:
      {status: ...}
      {data: {status: ...}}
      {data: {data: {status: ...}}}
      {result: {status: ...}}
    """
    core: Any = obj
    if isinstance(obj.get("data"), dict):
        core = obj["data"]
        if isinstance(core.get("data"), dict):
            core = core["data"]
    elif isinstance(obj.get("result"), dict):
        core = obj["result"]

    if not isinstance(core, dict):
        return {"raw": obj}
    return core


def _should_fallback_to_next_submit_path(path: str, status_code: int, body: str) -> bool:
    """
    Allow AV4 -> V2 fallback for endpoint rollout mismatch or AV4-specific
    validation mismatch, especially around audio fields.
    """
    p = str(path or "").strip()
    b = (body or "").lower()

    if status_code in (404, 405):
        return True

    if p.endswith("/v2/video/av4/generate") and status_code == 400:
        hints = [
            "audio_url",
            "audio_asset_id",
            "script and voice_id",
            "invalid_parameter",
            "talking_photo_id",
            "use_avatar_iv_model",
        ]
        if any(h in b for h in hints):
            return True

    return False


class HeyGenAV4Client:
    """
    Stable HeyGen client used by Fusion.

    Responsibilities:
      - upload talking-photo style source image (current bridge flow)
      - submit Avatar Video payloads
      - poll video status
      - fetch share URLs

    Note:
      - submit() remains generic for the current V2 create-video payload.
      - upload_talking_photo() is intentionally centralized here so the service
        layer and orchestrator do not drift on helper method names again.
    """
    provider_name = "heygen_av4"

    def __init__(self) -> None:
        self.base = settings.HEYGEN_BASE_URL.rstrip("/")
        self.timeout = settings.HEYGEN_TIMEOUT_SECONDS
        self.upload_base = str(os.getenv("HEYGEN_UPLOAD_BASE_URL", "https://upload.heygen.com")).rstrip("/")

    def _submit_paths(self) -> List[str]:
        env_path = str(os.getenv("HEYGEN_SUBMIT_PATH", "")).strip()
        if env_path:
            return [env_path]

        return [
            "/v2/video/av4/generate",
            "/v2/video/generate",
        ]

    def _enable_list_fallback(self) -> bool:
        v = str(os.getenv("HEYGEN_ENABLE_VIDEO_LIST_FALLBACK", "1")).strip().lower()
        return v in {"1", "true", "yes", "y"}

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.6, min=0.6, max=6.0),
        retry=retry_if_exception(_is_retryable_exception),
    )
    async def upload_talking_photo(self, image_path: str) -> str:
        """
        Upload a local face image and return a talking_photo_id.

        This keeps the current Fusion flow working while the product still uses
        a talking-photo style input for exact-audio videos.
        """
        api_key = getattr(settings, "HEYGEN_API_KEY", None)
        if not api_key:
            raise HeyGenApiError("HEYGEN_API_KEY is not set.")
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(image_path)

        content_type, _ = mimetypes.guess_type(str(path))
        if not content_type:
            content_type = "image/png"

        headers = {
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "Content-Type": content_type,
        }
        url = f"{self.upload_base}/v1/talking_photo"
        data = path.read_bytes()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, content=data)

        if resp.status_code >= 400:
            raise HeyGenApiError(f"HeyGen talking photo upload failed {resp.status_code}: {resp.text}")

        obj = _safe_json(resp)
        data_obj = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        talking_photo_id = (
            data_obj.get("talking_photo_id")
            or obj.get("talking_photo_id")
            or data_obj.get("id")
            or obj.get("id")
        )
        if not talking_photo_id:
            raise HeyGenApiError(f"HeyGen talking photo upload missing talking_photo_id: {obj}")
        return str(talking_photo_id).strip()

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.6, min=0.6, max=6.0),
        retry=retry_if_exception(_is_retryable_exception),
    )
    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        headers = _headers()
        headers["Idempotency-Key"] = idempotency_key

        last_error: Optional[str] = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for path in self._submit_paths():
                url = f"{self.base}{path}"
                try:
                    r = await client.post(url, headers=headers, json=payload)
                except (httpx.TimeoutException, httpx.TransportError):
                    raise
                except Exception as e:
                    last_error = f"{path}: transport/setup error: {e}"
                    logger.warning(
                        "heygen_submit_transport_error",
                        extra={"path": path, "error": str(e)},
                    )
                    continue

                if _should_fallback_to_next_submit_path(path, r.status_code, r.text):
                    body = (r.text or "")[:1000]
                    last_error = f"{path}: endpoint/payload mismatch ({r.status_code}): {body}"
                    logger.warning(
                        "heygen_submit_fallback_to_next_path",
                        extra={"path": path, "status_code": r.status_code, "body": body},
                    )
                    continue

                if r.status_code >= 400:
                    body = (r.text or "")[:1000]
                    last_error = f"{path}: HeyGen submit failed {r.status_code}: {body}"
                    logger.warning(
                        "heygen_submit_http_error",
                        extra={"path": path, "status_code": r.status_code, "body": body},
                    )
                    raise HeyGenApiError(last_error)

                data = _safe_json(r)
                provider_job_id = _extract_provider_job_id(data)
                if not provider_job_id:
                    raise HeyGenApiError(f"{path}: HeyGen submit missing video_id. Response: {data}")

                logger.info(
                    "heygen_submit_succeeded",
                    extra={
                        "submit_path": path,
                        "provider_job_id": provider_job_id,
                    },
                )
                return ProviderSubmitResult(provider_job_id=provider_job_id, raw_response=data)

        raise HeyGenApiError(last_error or "HeyGen submit failed on all candidate endpoints")

    @retry(
        reraise=True,
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=0.6, min=0.6, max=8.0),
        retry=retry_if_exception(_is_retryable_exception),
    )
    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        res = await self._poll_via_status(provider_job_id)
        if res is not None:
            return res

        if self._enable_list_fallback():
            return await self._poll_via_list(provider_job_id)

        return ProviderPollResult(
            status="processing",
            raw_response={"note": "status endpoint unavailable and list fallback disabled"},
        )

    async def _poll_via_status(self, provider_job_id: str) -> Optional[ProviderPollResult]:
        url = f"{self.base}/v1/video_status.get"
        headers = _headers()
        params = {"video_id": provider_job_id}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, headers=headers, params=params)

        if r.status_code in (404, 405):
            logger.info(
                "heygen_status_endpoint_unavailable",
                extra={"status_code": r.status_code, "provider_job_id": provider_job_id},
            )
            return None

        if r.status_code >= 400:
            raise HeyGenApiError(f"HeyGen video_status.get failed {r.status_code}: {r.text}")

        data = _safe_json(r)
        core = _unwrap_status_core(data)

        raw_status = core.get("status") or core.get("state") or core.get("video_status")
        status = _normalize_status(raw_status)
        video_url = _extract_video_url(core) or _extract_video_url(data)

        if video_url:
            return ProviderPollResult(status="succeeded", video_url=video_url, raw_response=core)

        if status == "failed":
            msg = _extract_error_message(core) or _extract_error_message(data) or "provider failed"
            return ProviderPollResult(status="failed", error_message=str(msg), raw_response=core)

        return ProviderPollResult(status="processing", raw_response=core)

    async def _poll_via_list(self, provider_job_id: str) -> ProviderPollResult:
        url = f"{self.base}/v1/video.list"
        headers = _headers()
        params = {"limit": 100}

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
            return ProviderPollResult(
                status="processing",
                raw_response={"note": "unexpected video.list shape", "response": data},
            )

        item = None
        for v in videos:
            if isinstance(v, dict):
                vid = v.get("video_id") or v.get("id")
                if str(vid) == str(provider_job_id):
                    item = v
                    break

        if item is None:
            return ProviderPollResult(
                status="processing",
                raw_response={"note": "video_id not found in list", "video_id": provider_job_id},
            )

        video_url = _extract_video_url(item)
        if video_url:
            return ProviderPollResult(status="succeeded", video_url=video_url, raw_response=item)

        status = _normalize_status(item.get("status") or item.get("state") or item.get("video_status"))

        if status == "failed":
            msg = _extract_error_message(item) or "provider failed"
            return ProviderPollResult(status="failed", error_message=str(msg), raw_response=item)

        return ProviderPollResult(status="processing", raw_response=item)

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.6, min=0.6, max=6.0),
        retry=retry_if_exception(_is_retryable_exception),
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
