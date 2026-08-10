from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx


class HedraApiError(RuntimeError):
    pass


@dataclass
class HedraGenerationSubmitResult:
    generation_id: str
    raw_response: Dict[str, Any]


class HedraClient:
    """
    Thin async client for Hedra's public API.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.api_key = str(api_key or os.getenv("HEDRA_API_KEY", "")).strip()
        self.base_url = str(base_url or os.getenv("HEDRA_BASE_URL", "https://api.hedra.com/web-app/public")).rstrip("/")
        self.timeout = float(timeout_seconds or os.getenv("HEDRA_TIMEOUT_SECONDS", "180"))
        if not self.api_key:
            raise HedraApiError("HEDRA_API_KEY is required")

        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"X-API-Key": self.api_key},
        )
        self._models_cache: Optional[List[Dict[str, Any]]] = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        content: Optional[bytes] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = await self._client.request(
                method.upper(),
                url,
                json=json_payload,
                headers=headers,
                content=content,
                params=params,
            )
        except Exception as exc:
            raise HedraApiError(f"network_error {method.upper()} {url}: {exc}") from exc

        if resp.status_code >= 400:
            detail = resp.text
            raise HedraApiError(f"HTTP {resp.status_code} for {method.upper()} {url}: {detail}")

        if not resp.content:
            return None
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype or resp.text.startswith("{") or resp.text.startswith("["):
            return resp.json()
        return resp.content

    async def list_models(self, *, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._models_cache is not None and not refresh:
            return self._models_cache
        data = await self._request("GET", "/models")
        models = data if isinstance(data, list) else list(data or [])
        self._models_cache = [m for m in models if isinstance(m, dict)]
        return self._models_cache

    async def get_credits(self) -> Dict[str, Any]:
        data = await self._request("GET", "/billing/credits")
        return data if isinstance(data, dict) else {}

    async def create_asset(self, *, name: str, asset_type: str) -> Dict[str, Any]:
        data = await self._request("POST", "/assets", json_payload={"name": name, "type": asset_type})
        return data if isinstance(data, dict) else {}

    async def upload_asset_bytes(
        self,
        *,
        asset_id: str,
        filename: str,
        data: bytes,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        boundary = f"----HedraBoundary{uuid.uuid4().hex}"
        ctype = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        preamble = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        closing = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = preamble + data + closing
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        result = await self._request("POST", f"/assets/{asset_id}/upload", headers=headers, content=body)
        return result if isinstance(result, dict) else {}

    async def submit_generation(self, payload: Dict[str, Any]) -> HedraGenerationSubmitResult:
        data = await self._request("POST", "/generations", json_payload=payload)
        raw = data if isinstance(data, dict) else {}
        generation_id = str(raw.get("id") or raw.get("generation_id") or "").strip()
        if not generation_id:
            raise HedraApiError(f"generation_id_missing_from_submit: {json.dumps(raw)}")
        return HedraGenerationSubmitResult(generation_id=generation_id, raw_response=raw)

    async def get_generation_status(self, generation_id: str) -> Dict[str, Any]:
        data = await self._request("GET", f"/generations/{generation_id}/status")
        return data if isinstance(data, dict) else {}

    async def generate_tts_audio_asset_id(
        self,
        *,
        text: str,
        voice_id: str,
        language: str = "English",
        stability: Optional[float] = None,
        speed: Optional[float] = None,
        model_id: Optional[str] = None,
        poll_seconds: int = 3,
        timeout_seconds: int = 300,
    ) -> str:
        payload: Dict[str, Any] = {
            "type": "text_to_speech",
            "voice_id": voice_id,
            "text": text,
            "language": language,
        }
        if model_id:
            payload["model_id"] = model_id
        if stability is not None:
            payload["stability"] = stability
        if speed is not None:
            payload["speed"] = speed

        submit = await self.submit_generation(payload)
        generation_id = submit.generation_id

        import time as _time

        cutoff = _time.time() + timeout_seconds
        last = {}
        while True:
            if _time.time() > cutoff:
                raise HedraApiError(f"tts_generation_timeout: {generation_id}")
            last = await self.get_generation_status(generation_id)
            status = str(last.get("status") or "").strip().lower()
            if status in {"complete", "completed", "success", "succeeded", "ready"}:
                asset_id = str(last.get("asset_id") or (last.get("asset") or {}).get("id") or "").strip()
                if not asset_id:
                    raise HedraApiError(f"tts_completed_without_asset_id: {json.dumps(last)}")
                return asset_id
            if status in {"failed", "error", "canceled", "cancelled"}:
                raise HedraApiError(f"tts_generation_failed: {json.dumps(last)}")
            await self._sleep(poll_seconds)

    async def fetch_bytes(self, url: str) -> tuple[bytes, str, str]:
        try:
            resp = await self._client.get(url)
        except Exception as exc:
            raise HedraApiError(f"download_failed {url}: {exc}") from exc
        if resp.status_code >= 400:
            raise HedraApiError(f"download_failed {url}: HTTP {resp.status_code} {resp.text}")
        parsed = urlparse(str(resp.url))
        filename = posixpath.basename(parsed.path) or f"asset_{uuid.uuid4().hex}"
        content_type = resp.headers.get("content-type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return resp.content, filename, content_type

    async def _sleep(self, seconds: int) -> None:
        import asyncio

        await asyncio.sleep(seconds)
