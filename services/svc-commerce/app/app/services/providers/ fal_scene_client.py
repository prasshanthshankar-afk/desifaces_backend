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
except Exception as e:  # pragma: no cover
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


class FalSceneClient:
    """
    Generic Fal Scene composition HTTP client.

    Configuration:
      - COMMERCE_FAL_SCENE_URL (preferred full URL)
        OR
      - FAL_API_BASE_URL + COMMERCE_FAL_SCENE_PATH

    Auth:
      - FAL_KEY / FAL_API_KEY
      - FAL_AUTH_MODE=key|bearer

    Polling support same as FalVTONClient.
    """

    def __init__(self) -> None:
        if httpx is None:
            raise RuntimeError("httpx not installed. Add 'httpx' to services/svc-commerce/app/app/requirements.txt")

        self.api_key = _env("FAL_KEY") or _env("FAL_API_KEY")
        self.base_url = _env("FAL_API_BASE_URL", "https://api.fal.ai").rstrip("/")
        self.scene_url = _env("COMMERCE_FAL_SCENE_URL").strip()
        self.scene_path = _env("COMMERCE_FAL_SCENE_PATH").strip()

        self.status_url_template = _env("COMMERCE_FAL_STATUS_URL_TEMPLATE").strip()
        self.timeout = _env_float("COMMERCE_FAL_HTTP_TIMEOUT_SECS", 60.0)
        self.poll_max = _env_float("COMMERCE_FAL_POLL_MAX_SECS", 180.0)
        self.poll_interval = _env_float("COMMERCE_FAL_POLL_INTERVAL_SECS", 2.0)

        if not self.api_key:
            raise RuntimeError("FalSceneClient: missing FAL_KEY / FAL_API_KEY")

        if not self.scene_url:
            if not self.scene_path:
                raise RuntimeError(
                    "FalSceneClient: missing COMMERCE_FAL_SCENE_URL (preferred) or COMMERCE_FAL_SCENE_PATH"
                )
            self.scene_url = urljoin(self.base_url + "/", self.scene_path.lstrip("/"))

    def _headers(self) -> Dict[str, str]:
        mode = (_env("FAL_AUTH_MODE", "key")).lower()
        if mode in ("bearer", "jwt"):
            auth = f"Bearer {self.api_key}"
        else:
            auth = f"Key {self.api_key}"
        return {"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"}

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

        rid = first.get("request_id") or first.get("id")
        if rid and self.status_url_template:
            return self.status_url_template.format(request_id=str(rid), id=str(rid))

        return None

    def _is_terminal(self, status: str) -> bool:
        s = (status or "").strip().lower()
        return s in ("succeeded", "success", "completed", "complete", "failed", "error", "canceled", "cancelled")

    async def generate(self, *, req: Any) -> Dict[str, Any]:
        """
        Called by SceneProvider via reflection: generate(req=req)
        Returns: {"urls": [...], "raw": <json>}
        """
        body = self._to_payload(req)
        first = await self._post_json(self.scene_url, body)

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

    def _to_payload(self, req: Any) -> Dict[str, Any]:
        d = _as_dict(getattr(req, "__dict__", None) or req)
        variants = []
        for v in d.get("variants") or []:
            vd = _as_dict(getattr(v, "__dict__", None) or v)
            variants.append(vd)

        # Scene compose must preserve labels/logos; product_assets should contain cutout/mask references.
        return {
            "kind": "commerce_scene",
            "user_id": str(d.get("user_id") or ""),
            "studio_job_id": str(d.get("studio_job_id") or ""),
            "commerce_campaign_id": str(d.get("commerce_campaign_id") or ""),
            "quote_id": str(d.get("quote_id") or ""),
            "request_hash": str(d.get("request_hash") or ""),
            "language": str(d.get("language") or "en"),
            "resolution": str(d.get("resolution") or "hd"),
            "product_assets": _as_dict(d.get("product_assets")),
            "variants": variants,
        }