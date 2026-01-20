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
    """
    Normalized result for svc-face pipeline.

    We return BYTES (not provider URLs) so the worker can always upload to
    DesiFaces storage (Azure blob) the same way for all providers.
    """
    bytes: bytes
    content_type: str  # "image/png", "image/jpeg", etc.
    provider: str      # "fal" or "openai"
    meta: Dict[str, Any]


class ImageProviderRouter:
    """
    One switchpoint for providers.

    settings/env:
      - DF_IMAGE_PROVIDER_DEFAULT = "fal" | "openai"
      - OPENAI_API_KEY (required if openai)
      - OPENAI_IMAGE_QUALITY (optional, default "high")
      - OPENAI_IMAGE_SIZE (optional, default "1024x1024")
      - OPENAI_IMAGE_MODEL_T2I / OPENAI_IMAGE_MODEL_EDIT (optional)

    Fal semantics (LOCKED in your FalClient):
      preservation_strength in [0..1] where 1.0 = preserve identity more (minimal change).
    """

    def __init__(self):
        self._fal = FalClient()
        self._openai = None  # lazy init

    # -----------------------------
    # internal helpers
    # -----------------------------
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

    # -----------------------------
    # Public API
    # -----------------------------
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
            # OpenAI client is sync (requests); run in a thread to avoid blocking event loop.
            img_bytes = await asyncio.to_thread(
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

        # Fal path (returns URL -> download bytes)
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
        # OpenAI edits need local files:
        src_local_path: Optional[str] = None,
        mask_local_path: Optional[str] = None,
        provider: Optional[ProviderName] = None,
    ) -> ImageBytesResult:
        """
        I2I/Edit:
          - Fal: uses image_url directly and your LOCKED preservation semantics.
          - OpenAI: requires src_local_path (download image_url to /tmp first). Optional mask_local_path.
        """
        p = self._pick_provider(provider)

        if p == "openai":
            oa = self._get_openai()
            if not src_local_path:
                raise RuntimeError(
                    "openai_edit_requires_src_local_path: download image_url to /tmp and pass src_local_path"
                )

            img_bytes = await asyncio.to_thread(
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

        # Fal I2I (CRITICAL: pass your LOCKED preservation semantics through as `strength`)
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