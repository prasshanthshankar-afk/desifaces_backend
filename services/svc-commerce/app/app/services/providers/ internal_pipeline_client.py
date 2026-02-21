from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    v = _env(name)
    if not v:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (bytes, bytearray)):
        try:
            x = x.decode("utf-8", errors="ignore")
        except Exception:
            return {}
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def _collect_urls(obj: Any) -> List[str]:
    out: List[str] = []

    def walk(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("http://") or s.startswith("https://"):
                out.append(s)
            return
        if isinstance(v, dict):
            for vv in v.values():
                walk(vv)
            return
        if isinstance(v, list):
            for vv in v:
                walk(vv)
            return

    walk(obj)
    seen = set()
    uniq: List[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


class InternalPipelineClient:
    """
    Internal pipeline client (your own GPU worker/service) for:
      - VTON (apparel)
      - Scene composition (FMCG/electronics)

    Config:
      - COMMERCE_INTERNAL_PIPELINE_URL or INTERNAL_PIPELINE_URL
      - COMMERCE_INTERNAL_VTON_PATH   (default: /api/commerce/vton)
      - COMMERCE_INTERNAL_SCENE_PATH  (default: /api/commerce/scene)
      - COMMERCE_INTERNAL_TOKEN (optional bearer token)

    Polling:
      - If response returns status_url/poll_url, we poll.
    """

    def __init__(self) -> None:
        if httpx is None:
            raise RuntimeError("httpx not installed. Add 'httpx' to services/svc-commerce/app/app/requirements.txt")

        self.base_url = (
            _env("COMMERCE_INTERNAL_PIPELINE_URL")
            or _env("INTERNAL_PIPELINE_URL")
            or "http://localhost:8010"
        ).rstrip("/")

        self.vton_path = _env("COMMERCE_INTERNAL_VTON_PATH", "/api/commerce/vton")
        self.scene_path = _env("COMMERCE_INTERNAL_SCENE_PATH", "/api/commerce/scene")

        self.token = _env("COMMERCE_INTERNAL_TOKEN") or _env("INTERNAL_PIPELINE_TOKEN")

        self.timeout = _env_float("COMMERCE_INTERNAL_HTTP_TIMEOUT_SECS", 90.0)
        self.poll_max = _env_float("COMMERCE_INTERNAL_POLL_MAX_SECS", 240.0)
        self.poll_interval = _env_float("COMMERCE_INTERNAL_POLL_INTERVAL_SECS", 2.0)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        assert httpx is not None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            return _as_dict(r.json())

    async def _get_json(self, url: str) -> Dict[str, Any]:
        assert httpx is not None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, headers=self._headers())
            r.raise_for_status()
            return _as_dict(r.json())

    def _infer_status_url(self, first: Dict[str, Any]) -> Optional[str]:
        for k in ("status_url", "poll_url", "request_url", "result_url"):
            v = first.get(k)
            if isinstance(v, str) and v.strip().startswith("http"):
                return v.strip()
        return None

    def _is_terminal(self, status: str) -> bool:
        s = (status or "").strip().lower()
        return s in ("succeeded", "success", "completed", "complete", "failed", "error", "canceled", "cancelled")

    async def generate_vton(self, *, req: Any) -> Dict[str, Any]:
        url = urljoin(self.base_url + "/", self.vton_path.lstrip("/"))
        body = self._to_payload(req, kind="commerce_vton")
        first = await self._post_json(url, body)

        urls = _collect_urls(first)
        if urls:
            return {"urls": urls, "raw": first}

        status_url = self._infer_status_url(first)
        if not status_url:
            return {"urls": [], "raw": first}

        t0 = time.time()
        last: Dict[str, Any] = first
        while time.time() - t0 <= self.poll_max:
            await self._sleep(self.poll_interval)
            last = await self._get_json(status_url)
            urls = _collect_urls(last)
            if urls:
                return {"urls": urls, "raw": last}

            status = str(last.get("status") or last.get("state") or "")
            if status and self._is_terminal(status):
                break

        return {"urls": [], "raw": last}

    async def generate_scene(self, *, req: Any) -> Dict[str, Any]:
        url = urljoin(self.base_url + "/", self.scene_path.lstrip("/"))
        body = self._to_payload(req, kind="commerce_scene")
        first = await self._post_json(url, body)

        urls = _collect_urls(first)
        if urls:
            return {"urls": urls, "raw": first}

        status_url = self._infer_status_url(first)
        if not status_url:
            return {"urls": [], "raw": first}

        t0 = time.time()
        last: Dict[str, Any] = first
        while time.time() - t0 <= self.poll_max:
            await self._sleep(self.poll_interval)
            last = await self._get_json(status_url)
            urls = _collect_urls(last)
            if urls:
                return {"urls": urls, "raw": last}

            status = str(last.get("status") or last.get("state") or "")
            if status and self._is_terminal(status):
                break

        return {"urls": [], "raw": last}

    async def _sleep(self, secs: float) -> None:
        import asyncio

        await asyncio.sleep(max(0.25, float(secs)))

    def _to_payload(self, req: Any, *, kind: str) -> Dict[str, Any]:
        d = _as_dict(getattr(req, "__dict__", None) or req)

        variants_out: List[Dict[str, Any]] = []
        for v in d.get("variants") or []:
            vd = _as_dict(getattr(v, "__dict__", None) or v)
            variants_out.append(vd)

        return {
            "kind": kind,
            "user_id": str(d.get("user_id") or ""),
            "studio_job_id": str(d.get("studio_job_id") or ""),
            "commerce_campaign_id": str(d.get("commerce_campaign_id") or ""),
            "quote_id": str(d.get("quote_id") or ""),
            "request_hash": str(d.get("request_hash") or ""),
            "language": str(d.get("language") or "en"),
            "resolution": str(d.get("resolution") or "hd"),
            "product_assets": _as_dict(d.get("product_assets")),
            "model_ref": _as_dict(d.get("model_ref")),
            "variants": variants_out,
        }