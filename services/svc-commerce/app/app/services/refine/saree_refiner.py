from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import uuid4

import requests

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _guess_content_type(path: str) -> str:
    p = path.lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    if p.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def _download(url: str, out_path: str, timeout_s: int = 120) -> None:
    with requests.get(url, stream=True, timeout=timeout_s) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


@dataclass
class SareeRefinerConfig:
    enabled: bool = True
    provider: str = "fal"  # "fal" | "internal"
    model: str = ""        # optional model id for fal/internal

    @staticmethod
    def from_env() -> "SareeRefinerConfig":
        return SareeRefinerConfig(
            enabled=_env_bool("DF_SAREE_REFINE_ENABLED", True),
            provider=(_env("DF_SAREE_REFINE_PROVIDER", "fal").lower() or "fal"),
            model=_env("DF_SAREE_REFINE_MODEL", ""),
        )


class SareeRefiner:
    """
    2D refinement pass (MVP):
    - input: local image path (composed base)
    - output: local image path (refined)
    """

    def __init__(
        self,
        *,
        storage: Any = None,             # AzureStorageService (optional, helps when provider needs URLs)
        fal_scene_client: Any = None,     # FalSceneClient (optional)
        internal_client: Any = None,      # InternalPipelineClient (optional)
        config: Optional[SareeRefinerConfig] = None,
    ) -> None:
        self.storage = storage
        self.fal = fal_scene_client
        self.internal = internal_client
        self.cfg = config or SareeRefinerConfig.from_env()

    def refine(
        self,
        *,
        image_path: str,
        prompt: str,
        seed: int,
        steps: int = 28,
        strength: float = 0.55,
        debug: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if not self.cfg.enabled:
            if debug is not None:
                debug["refine"] = "disabled"
            return None

        # Many providers prefer a URL; upload to temp if possible
        image_url = None
        if self.storage:
            blob = f"commerce/vton/tmp/refine_inputs/{uuid4().hex}.png"
            ct = _guess_content_type(image_path)
            image_url = self._upload_any(image_path=image_path, blob_path=blob, content_type=ct)
        if debug is not None:
            debug["refine_input_url"] = image_url

        # Try INTERNAL first if configured
        if self.cfg.provider == "internal":
            out_url = self._try_internal(prompt=prompt, image_url=image_url, seed=seed, steps=steps, strength=strength, debug=debug)
            if out_url:
                return self._download_out(out_url)

        # Default: FAL
        out_url = self._try_fal(prompt=prompt, image_url=image_url, seed=seed, steps=steps, strength=strength, debug=debug)
        if out_url:
            return self._download_out(out_url)

        if debug is not None:
            debug["refine"] = "failed_no_provider_output"
        return None

    def _download_out(self, out_url: str) -> Optional[str]:
        try:
            with tempfile.TemporaryDirectory(prefix="df_saree_refine_") as td:
                out_path = os.path.join(td, "refined.png")
                _download(out_url, out_path)
                # return a persisted copy (caller tempdir might go away)
                final_path = os.path.join(tempfile.gettempdir(), f"df_refined_{uuid4().hex}.png")
                with open(out_path, "rb") as src, open(final_path, "wb") as dst:
                    dst.write(src.read())
                return final_path
        except Exception as e:
            logger.warning("refine download failed: %s", e)
            return None

    def _upload_any(self, *, image_path: str, blob_path: str, content_type: str) -> Optional[str]:
        for method_name in ("upload_file", "upload_path", "upload_local_file", "upload"):
            m = getattr(self.storage, method_name, None)
            if callable(m):
                try:
                    url = m(image_path, blob_path, content_type=content_type)  # type: ignore[misc]
                    if isinstance(url, str) and url:
                        return url
                except TypeError:
                    url = m(image_path, blob_path)  # type: ignore[misc]
                    if isinstance(url, str) and url:
                        return url
        return None

    def _try_internal(
        self,
        *,
        prompt: str,
        image_url: Optional[str],
        seed: int,
        steps: int,
        strength: float,
        debug: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not self.internal:
            if debug is not None:
                debug["refine_internal"] = "missing_client"
            return None

        # Try common method names
        payload = {
            "prompt": prompt,
            "image_url": image_url,
            "seed": seed,
            "steps": steps,
            "strength": strength,
            "model": self.cfg.model or None,
        }

        for method_name in ("refine_image", "img2img", "image_to_image", "run_img2img", "run"):
            m = getattr(self.internal, method_name, None)
            if callable(m):
                try:
                    resp = m(payload)  # type: ignore[misc]
                    out_url = self._extract_url(resp)
                    if debug is not None:
                        debug["refine_internal"] = {"method": method_name, "out_url": out_url}
                    return out_url
                except Exception as e:
                    if debug is not None:
                        debug["refine_internal_error"] = f"{method_name}: {e}"
        return None

    def _try_fal(
        self,
        *,
        prompt: str,
        image_url: Optional[str],
        seed: int,
        steps: int,
        strength: float,
        debug: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not self.fal:
            if debug is not None:
                debug["refine_fal"] = "missing_client"
            return None

        payload = {
            "prompt": prompt,
            "image_url": image_url,
            "seed": seed,
            "num_inference_steps": steps,
            "strength": strength,
            "model": self.cfg.model or None,
        }

        for method_name in ("img2img", "image_to_image", "run_img2img", "run"):
            m = getattr(self.fal, method_name, None)
            if callable(m):
                try:
                    resp = m(payload)  # type: ignore[misc]
                    out_url = self._extract_url(resp)
                    if debug is not None:
                        debug["refine_fal"] = {"method": method_name, "out_url": out_url}
                    return out_url
                except Exception as e:
                    if debug is not None:
                        debug["refine_fal_error"] = f"{method_name}: {e}"
        return None

    def _extract_url(self, resp: Any) -> Optional[str]:
        if isinstance(resp, str) and resp.startswith("http"):
            return resp
        if isinstance(resp, dict):
            for k in ("output_url", "url", "image_url"):
                v = resp.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            # common fal shape: {"images":[{"url":...}]}
            imgs = resp.get("images")
            if isinstance(imgs, list) and imgs:
                u = (imgs[0] or {}).get("url")
                if isinstance(u, str) and u.startswith("http"):
                    return u
        return None