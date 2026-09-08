from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Literal, Dict, Any, Tuple

import httpx

from app.config import settings
from app.services.fal_client import FalClient

logger = logging.getLogger(__name__)

ProviderName = Literal["fal", "openai"]


@dataclass(frozen=True)
class ImageBytesResult:
    """Normalized provider result returned as bytes for canonical blob persistence."""
    bytes: bytes
    content_type: str
    provider: str
    meta: Dict[str, Any]


class ImageProviderRouter:
    """Single provider switchpoint for Face T2I and I2I generation."""

    def __init__(self):
        self._fal = FalClient()
        self._openai = None

    @staticmethod
    def _pick_provider(explicit: Optional[str] = None) -> ProviderName:
        p = (explicit or getattr(settings, "DF_IMAGE_PROVIDER_DEFAULT", "fal") or "fal").lower()
        return "openai" if p == "openai" else "fal"

    @staticmethod
    async def _download_url_bytes(url: str, *, timeout_s: float = 120.0) -> Tuple[bytes, str]:
        if not url:
            raise RuntimeError("provider_returned_empty_url")
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type") or "application/octet-stream"
            return r.content, ct

    def _get_openai(self):
        if self._openai is not None:
            return self._openai
        try:
            from app.services.providers.openai_image_client import OpenAIImageClient
        except Exception as e:
            raise RuntimeError(
                "openai_provider_selected_but_openai_image_client_missing: "
                "create app/services/providers/openai_image_client.py"
            ) from e
        self._openai = OpenAIImageClient()
        return self._openai

    @staticmethod
    def _transient_openai_error(exc: Exception) -> bool:
        # OPENAI_IMAGE_TRANSIENT_RETRY_V1_DEV_SYNC: production retries the same
        # provider statuses in OpenAIImageClient. Keep the dev baseline protected
        # as well so a future rebuild cannot regress to single-attempt behavior.
        text = str(exc or "").lower()
        return any(token in text for token in ("status=429", "status=500", "status=502", "status=503", "status=504"))

    async def _openai_call_with_retry(self, fn, /, **kwargs):
        attempts = 3
        delay = 1.5
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.to_thread(fn, **kwargs)
            except Exception as exc:
                last = exc
                if attempt >= attempts or not self._transient_openai_error(exc):
                    raise
                logger.warning(
                    "Transient OpenAI image provider failure; retrying",
                    extra={"attempt": attempt, "max_attempts": attempts, "error": str(exc)},
                )
                await asyncio.sleep(min(delay * (2 ** (attempt - 1)), 15.0))
        if last:
            raise last
        raise RuntimeError("openai_image_retry_exhausted")

    async def generate_t2i_bytes(
        self,
        *,
        prompt: str,
        negative_prompt: Optional[str] = None,
        seed: int = 0,
        width: int = 1024,
        height: int = 1024,
        num_inference_steps: int = 28,
        guidance_scale: float = 3.5,
        provider: Optional[ProviderName] = None,
    ) -> ImageBytesResult:
        p = self._pick_provider(provider)

        if p == "openai":
            oa = self._get_openai()
            img_bytes = await self._openai_call_with_retry(
                oa.generate_image,
                prompt=prompt,
                size=f"{width}x{height}",
                quality=getattr(settings, "OPENAI_IMAGE_QUALITY", None) or "high",
            )
            return ImageBytesResult(
                bytes=img_bytes,
                content_type="image/png",
                provider="openai",
                meta={"mode": "t2i"},
            )

        result = await self._fal.generate_image(
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            width=width,
            height=height,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
        )
        url = str(result.get("url") or "")
        b, ct = await self._download_url_bytes(url)
        return ImageBytesResult(
            bytes=b,
            content_type=str(result.get("content_type") or ct or "image/jpeg"),
            provider="fal",
            meta={"mode": "t2i", "provider_url": url, "raw": result.get("raw")},
        )

    async def generate_i2i_bytes(
        self,
        *,
        prompt: str,
        image_url: str,
        negative_prompt: Optional[str] = None,
        seed: Optional[int] = None,
        width: int = 1024,
        height: int = 1024,
        num_inference_steps: int = 40,
        guidance_scale: float = 0.0,
        preservation_strength: float = 0.75,
        src_local_path: Optional[str] = None,
        mask_local_path: Optional[str] = None,
        provider: Optional[ProviderName] = None,
    ) -> ImageBytesResult:
        p = self._pick_provider(provider)

        if p == "openai":
            oa = self._get_openai()
            if not src_local_path:
                raise RuntimeError(
                    "openai_edit_requires_src_local_path: download image_url to /tmp and pass src_local_path"
                )

            img_bytes = await self._openai_call_with_retry(
                oa.edit_image,
                prompt=prompt,
                image_path=src_local_path,
                mask_path=mask_local_path,
                size=f"{width}x{height}",
                quality=getattr(settings, "OPENAI_IMAGE_QUALITY", None) or "high",
            )
            return ImageBytesResult(
                bytes=img_bytes,
                content_type="image/png",
                provider="openai",
                meta={
                    "mode": "i2i_edit",
                    "seed": seed,
                    "preservation_strength": float(preservation_strength),
                    "used_mask": bool(mask_local_path),
                },
            )

        result = await self._fal.generate_image_to_image(
            prompt=prompt,
            negative_prompt=negative_prompt,
            image_url=image_url,
            strength=float(preservation_strength),
            seed=seed,
            width=width,
            height=height,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
        )
        url = str(result.get("url") or "")
        b, ct = await self._download_url_bytes(url)
        return ImageBytesResult(
            bytes=b,
            content_type=str(result.get("content_type") or ct or "image/jpeg"),
            provider="fal",
            meta={
                "mode": "i2i",
                "seed": seed,
                "preservation_strength": float(preservation_strength),
                "provider_url": url,
                "raw": result.get("raw"),
            },
        )
