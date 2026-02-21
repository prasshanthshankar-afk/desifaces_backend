from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

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


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return []


def _looks_like_output_url(u: str) -> bool:
    """
    Heuristic: output URLs are usually CDN/media, and often end with image/video extensions.
    We explicitly avoid returning queue/status/cancel endpoints as "outputs".
    """
    s = (u or "").strip()
    if not (s.startswith("http://") or s.startswith("https://")):
        return False

    # never treat queue endpoints as outputs
    host = (urlparse(s).netloc or "").lower()
    if "queue.fal.run" in host or host.endswith("fal.run"):
        return False

    # strong signal: fal media CDN
    if "fal.media" in host:
        return True

    # fallback: file extension
    lowered = s.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov"):
        if ext in lowered:
            return True

    return False


def _extract_media_urls(obj: Any) -> List[str]:
    """
    Prefer schema-aware extraction:
      - {"image": {"url": ...}}
      - {"images": [{"url": ...}, ...]}
    Fallback: recursive walk with output-url heuristic.
    """
    out: List[str] = []

    d = _as_dict(obj)

    img = _as_dict(d.get("image"))
    u = (img.get("url") or "").strip()
    if _looks_like_output_url(u):
        return [u]

    imgs = _as_list(d.get("images"))
    for it in imgs:
        uu = (_as_dict(it).get("url") or "").strip()
        if _looks_like_output_url(uu):
            out.append(uu)

    if out:
        # de-dupe preserve order
        seen = set()
        uniq: List[str] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    # fallback recursive
    found: List[str] = []

    def walk(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str):
            s = v.strip()
            if _looks_like_output_url(s):
                found.append(s)
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
    uniq2: List[str] = []
    for x in found:
        if x not in seen:
            seen.add(x)
            uniq2.append(x)
    return uniq2


class FalVTONClient:
    """
    Fal queue VTON client (CatVTON-compatible).

    Required:
      - FAL_KEY (or FAL_API_KEY)
      - COMMERCE_FAL_VTON_URL = https://queue.fal.run/fal-ai/cat-vton   (queue submit)

    Polling:
      - submit response returns status_url + response_url
      - poll status_url until COMPLETED
      - then GET response_url for final output JSON (contains image.url)
    """

    def __init__(self) -> None:
        if httpx is None:
            raise RuntimeError("httpx not installed. Add 'httpx' to services/svc-commerce/app/app/requirements.txt")

        self.api_key = _env("FAL_KEY") or _env("FAL_API_KEY")
        self.base_url = _env("FAL_API_BASE_URL", "https://queue.fal.run").rstrip("/")
        self.vton_url = _env("COMMERCE_FAL_VTON_URL").strip()
        self.vton_path = _env("COMMERCE_FAL_VTON_PATH").strip()

        self.timeout = _env_float("COMMERCE_FAL_HTTP_TIMEOUT_SECS", 60.0)
        self.poll_max = _env_float("COMMERCE_FAL_POLL_MAX_SECS", 180.0)
        self.poll_interval = _env_float("COMMERCE_FAL_POLL_INTERVAL_SECS", 2.0)

        if not self.api_key:
            raise RuntimeError("FalVTONClient: missing FAL_KEY / FAL_API_KEY")

        if not self.vton_url:
            if not self.vton_path:
                raise RuntimeError(
                    "FalVTONClient: missing COMMERCE_FAL_VTON_URL (preferred) or COMMERCE_FAL_VTON_PATH"
                )
            self.vton_url = urljoin(self.base_url + "/", self.vton_path.lstrip("/"))

    def _headers(self) -> Dict[str, str]:
        mode = (_env("FAL_AUTH_MODE", "key")).lower()
        if mode in ("bearer", "jwt"):
            auth = f"Bearer {self.api_key}"
        else:
            auth = f"Key {self.api_key}"
        return {
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        assert httpx is not None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, headers=self._headers(), json=body)
            try:
                j = r.json()
            except Exception:
                j = {"_non_json_body": (r.text or "")[:800]}
            if r.status_code >= 400:
                raise RuntimeError(f"FalVTONClient: POST failed status={r.status_code} url={url} json={j}")
            return _as_dict(j)

    async def _get_json(self, url: str) -> Dict[str, Any]:
        assert httpx is not None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, headers=self._headers())
            try:
                j = r.json()
            except Exception:
                j = {"_non_json_body": (r.text or "")[:800]}
            if r.status_code >= 400:
                raise RuntimeError(f"FalVTONClient: GET failed status={r.status_code} url={url} json={j}")
            return _as_dict(j)

    def _infer_queue_urls(self, first: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        status_url = first.get("status_url")
        response_url = first.get("response_url")
        su = status_url.strip() if isinstance(status_url, str) else None
        ru = response_url.strip() if isinstance(response_url, str) else None
        return (su if su and su.startswith("http") else None, ru if ru and ru.startswith("http") else None)

    def _status_value(self, d: Dict[str, Any]) -> str:
        return str(d.get("status") or d.get("state") or "").strip().upper()

    async def generate(self, *, req: Any) -> Dict[str, Any]:
        """
        Called by VTONProvider via reflection as: generate(req=req)

        Returns:
          {"urls": [...], "raw": <final_json>, "submit": <submit_json>}
        """
        body = self._to_payload(req)
        submit = await self._post_json(self.vton_url, body)

        # Some endpoints may return outputs immediately (sync-like). Try extract.
        immediate = _extract_media_urls(submit)
        status_url, response_url = self._infer_queue_urls(submit)

        if immediate and not status_url:
            return {"urls": immediate, "raw": submit, "submit": submit}

        if not status_url:
            # No outputs and no polling handle — return submit for debugging.
            return {"urls": [], "raw": submit, "submit": submit}

        t0 = time.time()
        last_status: Dict[str, Any] = submit

        while time.time() - t0 <= self.poll_max:
            await self._sleep(self.poll_interval)
            last_status = await self._get_json(status_url)

            # Sometimes status endpoint itself might include output (rare, but handle)
            urls = _extract_media_urls(last_status)
            if urls:
                return {"urls": urls, "raw": last_status, "submit": submit}

            st = self._status_value(last_status)
            if st in ("FAILED", "ERROR", "CANCELED", "CANCELLED"):
                raise RuntimeError(f"FalVTONClient: job failed status_json={last_status}")

            if st in ("COMPLETED", "COMPLETE", "SUCCEEDED", "SUCCESS"):
                if response_url:
                    final_json = await self._get_json(response_url)
                    final_urls = _extract_media_urls(final_json)
                    if not final_urls:
                        # FAL sometimes returns validation detail here if payload wrong
                        if final_json.get("detail"):
                            raise RuntimeError(f"FalVTONClient: completed but response has detail={final_json.get('detail')}")
                        raise RuntimeError(f"FalVTONClient: completed but no media urls in response={final_json}")
                    return {"urls": final_urls, "raw": final_json, "submit": submit}

                # If we have no response_url, return status JSON (best effort)
                return {"urls": [], "raw": last_status, "submit": submit}

        raise RuntimeError(f"FalVTONClient: timeout after {self.poll_max}s last_status={last_status}")

    async def _sleep(self, secs: float) -> None:
        import asyncio

        await asyncio.sleep(max(0.25, float(secs)))

    def _to_payload(self, req: Any) -> Dict[str, Any]:
        """
        Convert your VTONGenerateRequest into CatVTON-compatible payload.

        CatVTON expects (top-level):
          human_image_url, garment_image_url, cloth_type, (optional seed)

        We support:
          - req already dict with those keys
          - req.variants[0] having those keys
          - req having those keys directly
        """
        d = _as_dict(getattr(req, "__dict__", None) or req)

        # direct keys
        if d.get("human_image_url") and d.get("garment_image_url") and d.get("cloth_type"):
            out = {
                "human_image_url": str(d["human_image_url"]),
                "garment_image_url": str(d["garment_image_url"]),
                "cloth_type": str(d["cloth_type"]),
            }
            if d.get("seed") is not None:
                out["seed"] = int(d["seed"])
            return out

        # variants[0]
        variants = []
        for v in d.get("variants") or []:
            vd = _as_dict(getattr(v, "__dict__", None) or v)
            variants.append(vd)

        if variants:
            v0 = variants[0]
            if v0.get("human_image_url") and v0.get("garment_image_url") and v0.get("cloth_type"):
                out2 = {
                    "human_image_url": str(v0["human_image_url"]),
                    "garment_image_url": str(v0["garment_image_url"]),
                    "cloth_type": str(v0["cloth_type"]),
                }
                if v0.get("seed") is not None:
                    out2["seed"] = int(v0["seed"])
                return out2

        # last resort: pass-through (debug). This will likely fail if schema mismatch.
        return d