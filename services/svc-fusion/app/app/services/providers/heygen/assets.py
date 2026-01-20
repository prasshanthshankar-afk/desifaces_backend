from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import httpx

from app.config import settings
from app.services.providers.heygen.client import HeyGenApiError

logger = logging.getLogger("heygen_assets")


def _upload_base() -> str:
    # allow override if you ever need it
    return getattr(settings, "HEYGEN_UPLOAD_BASE_URL", None) or "https://upload.heygen.com"


def _safe_json(r: httpx.Response) -> Dict[str, Any]:
    """
    HeyGen sometimes returns 200 with empty/invalid body transiently.
    We surface a clear error instead of crashing with JSONDecodeError.
    """
    try:
        return r.json()
    except Exception:
        body = (r.text or "").strip()
        snippet = body[:500] if body else "<EMPTY_BODY>"
        raise HeyGenApiError(f"Invalid JSON from HeyGen upload endpoint (status={r.status_code}): {snippet}")


def extract_image_key(upload_res: Dict[str, Any]) -> str:
    """
    Extract HeyGen image_key from Upload Asset response.

    Expected:
      {"code":100,"data":{"image_key":"image/<id>/original.jpg", ...}}
    """
    data = upload_res.get("data") or upload_res
    image_key = data.get("image_key") or data.get("asset_key") or data.get("key")
    if not image_key:
        raise HeyGenApiError(f"HeyGen image upload missing image_key: {upload_res}")
    return str(image_key)


def extract_audio_asset(upload_res: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    Extract HeyGen audio asset id + url from Upload Asset response.

    Expected:
      {"code":100,"data":{"id":"...","file_type":"audio","url":"https://.../original.mp3"}}
    """
    data = upload_res.get("data") or upload_res
    audio_id = data.get("id") or data.get("asset_id") or data.get("key")
    audio_url = data.get("url")
    if not audio_id:
        raise HeyGenApiError(f"HeyGen audio upload missing id: {upload_res}")
    return str(audio_id), (str(audio_url) if audio_url else None)


def extract_talking_photo_id(upload_res: Dict[str, Any]) -> str:
    """
    Extract talking_photo_id from HeyGen /v1/talking_photo response.

    Expected (per HeyGen patterns):
      {"code":100,"data":{"talking_photo_id":"..."}}
    """
    data = upload_res.get("data") or upload_res
    tpid = data.get("talking_photo_id") or data.get("id")
    if not tpid:
        raise HeyGenApiError(f"HeyGen talking_photo upload missing talking_photo_id: {upload_res}")
    return str(tpid)


class HeyGenAssetsClient:
    def __init__(self) -> None:
        self.timeout = settings.HEYGEN_TIMEOUT_SECONDS

    def _headers(self) -> Dict[str, str]:
        if not settings.HEYGEN_API_KEY:
            raise HeyGenApiError("HEYGEN_API_KEY is not set.")
        return {"X-Api-Key": settings.HEYGEN_API_KEY, "Accept": "application/json"}

    async def _download(self, url: str) -> Tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            r = await client.get(url)
        if r.status_code >= 400:
            raise HeyGenApiError(f"Failed to download {r.status_code}: {r.text[:300]}")
        content_type = (r.headers.get("content-type") or "").split(";")[0].strip()
        return r.content, content_type

    async def upload_image_asset_from_url(self, url: str) -> Dict[str, Any]:
        """
        Deterministic image upload:

          POST {UPLOAD_BASE}/v1/asset
          Content-Type: image/jpeg
          Body: raw bytes

        Returns JSON containing data.image_key.
        """
        content, content_type = await self._download(url)
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"

        upload_base = _upload_base()
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            r = await client.post(
                f"{upload_base}/v1/asset",
                headers={**self._headers(), "Content-Type": content_type},
                content=content,
            )

        if r.status_code >= 400:
            raise HeyGenApiError(f"HeyGen image upload failed {r.status_code}: {r.text[:800]}")

        data = _safe_json(r)
        _ = extract_image_key(data)
        return data

    async def upload_audio_asset_from_url(self, url: str) -> Dict[str, Any]:
        """
        Deterministic audio upload:

          POST {UPLOAD_BASE}/v1/asset
          Content-Type: audio/mpeg (or source type)
          Body: raw bytes

        Returns JSON containing data.id and usually data.url.
        """
        content, content_type = await self._download(url)
        if not content_type:
            content_type = "audio/mpeg"

        upload_base = _upload_base()
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            r = await client.post(
                f"{upload_base}/v1/asset",
                headers={**self._headers(), "Content-Type": content_type},
                content=content,
            )

        if r.status_code >= 400:
            raise HeyGenApiError(f"HeyGen audio upload failed {r.status_code}: {r.text[:800]}")

        data = _safe_json(r)
        _ = extract_audio_asset(data)
        return data

    async def upload_talking_photo_from_url(self, url: str) -> Dict[str, Any]:
        """
        Upload a 'talking photo' (preferred for AV4 avatar IV flows):

          POST {UPLOAD_BASE}/v1/talking_photo
          Content-Type: image/jpeg (or image/png)
          Body: raw bytes

        Returns JSON containing data.talking_photo_id.
        """
        content, content_type = await self._download(url)
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"

        upload_base = _upload_base()
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            r = await client.post(
                f"{upload_base}/v1/talking_photo",
                headers={**self._headers(), "Content-Type": content_type},
                content=content,
            )

        if r.status_code >= 400:
            raise HeyGenApiError(f"HeyGen talking_photo upload failed {r.status_code}: {r.text[:800]}")

        data = _safe_json(r)
        _ = extract_talking_photo_id(data)
        return data