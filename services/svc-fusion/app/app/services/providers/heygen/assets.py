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
        out = r.json()
        return out if isinstance(out, dict) else {"raw": out}
    except Exception:
        body = (r.text or "").strip()
        snippet = body[:500] if body else "<EMPTY_BODY>"
        raise HeyGenApiError(
            f"Invalid JSON from HeyGen upload endpoint (status={r.status_code}): {snippet}"
        )


def _pick_first_nonempty(*values: Any) -> Optional[str]:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def extract_image_key(upload_res: Dict[str, Any]) -> str:
    """
    Extract HeyGen image_key from Upload Asset response.

    Expected:
      {"code":100,"data":{"image_key":"image/<id>/original.jpg", ...}}
    """
    data = upload_res.get("data") if isinstance(upload_res.get("data"), dict) else upload_res
    image_key = _pick_first_nonempty(
        data.get("image_key"),
        data.get("asset_key"),
        data.get("key"),
    )
    if not image_key:
        raise HeyGenApiError(f"HeyGen image upload missing image_key: {upload_res}")
    return image_key


def extract_audio_asset(upload_res: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    Extract HeyGen audio asset id + url from Upload Asset response.

    Expected:
      {"code":100,"data":{"id":"...","file_type":"audio","url":"https://.../original.mp3"}}
    """
    data = upload_res.get("data") if isinstance(upload_res.get("data"), dict) else upload_res
    audio_id = _pick_first_nonempty(
        data.get("id"),
        data.get("asset_id"),
        data.get("key"),
    )
    audio_url = _pick_first_nonempty(data.get("url"))
    if not audio_id:
        raise HeyGenApiError(f"HeyGen audio upload missing id: {upload_res}")
    return audio_id, audio_url


def extract_talking_photo_id(upload_res: Dict[str, Any]) -> str:
    """
    Extract talking_photo_id from HeyGen talking-photo style responses.

    Accepts variants like:
      {"code":100,"data":{"talking_photo_id":"..."}}
      {"data":{"photo_avatar_id":"..."}}
      {"data":{"avatar_id":"..."}}

    We do NOT treat plain image_key as a talking_photo_id.
    We only fall back to generic data.id / id if image_key is absent.
    """
    data = upload_res.get("data") if isinstance(upload_res.get("data"), dict) else {}

    talking_photo_id = _pick_first_nonempty(
        data.get("talking_photo_id"),
        upload_res.get("talking_photo_id"),
        data.get("photo_avatar_id"),
        upload_res.get("photo_avatar_id"),
        data.get("avatar_id"),
        upload_res.get("avatar_id"),
    )
    if talking_photo_id:
        return talking_photo_id

    # only trust generic id if image_key is not present
    has_image_key = bool(
        _pick_first_nonempty(
            data.get("image_key"),
            upload_res.get("image_key"),
            data.get("asset_key"),
            upload_res.get("asset_key"),
            data.get("key"),
            upload_res.get("key"),
        )
    )
    if not has_image_key:
        generic_id = _pick_first_nonempty(data.get("id"), upload_res.get("id"))
        if generic_id:
            return generic_id

    raise HeyGenApiError(f"HeyGen talking_photo upload missing talking_photo_id: {upload_res}")


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

    async def _post_binary(self, path: str, content: bytes, content_type: str) -> Dict[str, Any]:
        upload_base = _upload_base().rstrip("/")
        url = f"{upload_base}{path}"

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            r = await client.post(
                url,
                headers={**self._headers(), "Content-Type": content_type},
                content=content,
            )

        if r.status_code >= 400:
            body = (r.text or "")[:800]
            raise HeyGenApiError(f"HeyGen upload failed {r.status_code} {path}: {body}")

        return _safe_json(r)

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

        data = await self._post_binary("/v1/asset", content, content_type)
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

        data = await self._post_binary("/v1/asset", content, content_type)
        _ = extract_audio_asset(data)
        return data

    async def upload_talking_photo_from_url(self, url: str) -> Dict[str, Any]:
        """
        Upload a talking photo for photo-avatar / avatar-IV flows:

          POST {UPLOAD_BASE}/v1/talking_photo
          Content-Type: image/jpeg (or image/png)
          Body: raw bytes

        Returns JSON containing a talking-photo style identifier.
        """
        content, content_type = await self._download(url)
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"

        data = await self._post_binary("/v1/talking_photo", content, content_type)
        _ = extract_talking_photo_id(data)
        return data

    async def create_talking_photo_from_url(self, url: str) -> Dict[str, Any]:
        """
        Preferred high-level helper for Fusion.

        This attempts to create / upload a talking photo directly and guarantees
        the response contains a real talking_photo style id, not only image_key.
        """
        data = await self.upload_talking_photo_from_url(url)
        talking_photo_id = extract_talking_photo_id(data)

        logger.info(
            "heygen_talking_photo_created",
            extra={
                "talking_photo_id": talking_photo_id,
            },
        )
        return data