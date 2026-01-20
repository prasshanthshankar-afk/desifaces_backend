from __future__ import annotations

from typing import Dict, Any, Optional
import os
import logging

import fal_client
from app.config import settings

logger = logging.getLogger(__name__)


class FalClient:
    """
    Client for fal.ai Flux image generation.

    Contract:
      - generate_image(): text-to-image
      - generate_image_to_image(): image-to-image (identity lock / editing)
      - generate_image_img2img(): alias for generate_image_to_image

    Normalized return:
      {
        "url": "https://...",
        "width": 1024,
        "height": 1024,
        "content_type": "image/jpeg",
        "raw": <provider response>
      }

    IMPORTANT (LOCKED PRODUCT SEMANTICS):
      svc-face orchestrator passes `preservation_strength` as `strength`.
      We keep that signature for compatibility.

      preservation_strength meaning (LOCKED):
        - 1.0 => preserve identity a lot => minimal change
        - 0.0 => preserve less => more change

    Provider endpoint notes (Fal OpenAPI / observed):
      - kontext(/max): prompt,image_url,guidance_scale,enhance_prompt,aspect_ratio,seed,...
        (NO strength, NO image_size, NO num_inference_steps, NO negative_prompt)
        => We can't directly control "how much to change" via a strength knob; we do best-effort
           by adjusting guidance_scale and adding preservation instructions in prompt.
      - redux: prompt,image_url,image_size,num_inference_steps,guidance_scale,enhance_prompt,seed,...
        (NO strength, NO image_prompt_strength)
        => Same: best-effort via guidance_scale + prompt instruction.
      - dev image-to-image: prompt,image_url,strength,image_size,num_inference_steps,guidance_scale,seed,...
        => True strength control. This is the recommended model for identity-lock I2I tests.
    """

    def __init__(self):
        self.api_key = (
            getattr(settings, "FAL_API_KEY", None)
            or os.getenv("FAL_API_KEY")
            or os.getenv("FAL_KEY")
        )
        if not self.api_key:
            raise RuntimeError("missing_fal_api_key: set FAL_API_KEY (or FAL_KEY)")

        fal_client.api_key = self.api_key

        self.model_t2i = getattr(settings, "FAL_MODEL", None) or os.getenv("FAL_MODEL")
        if not self.model_t2i:
            raise RuntimeError("missing_fal_model: set FAL_MODEL in settings/env")

        # Recommended default for edit-focused I2I (kontext), but note: NO strength knob.
        self.model_i2i = (
            getattr(settings, "FAL_I2I_MODEL", None)
            or os.getenv("FAL_I2I_MODEL")
            or "fal-ai/flux-pro/kontext/max"
        )

        # LOCKED semantics default: "preserve identity a lot" should be the default.
        self.default_preservation_strength = float(
            getattr(settings, "FAL_I2I_DEFAULT_STRENGTH", None)
            or os.getenv("FAL_I2I_DEFAULT_STRENGTH")
            or 0.75
        )

        # For kontext edits (clothing changes), 3.5 is often too weak.
        self.i2i_guidance_default = float(
            getattr(settings, "FAL_I2I_GUIDANCE_SCALE_DEFAULT", None)
            or os.getenv("FAL_I2I_GUIDANCE_SCALE_DEFAULT")
            or 7.5
        )

        self.i2i_enhance_prompt_default = str(
            getattr(settings, "FAL_I2I_ENHANCE_PROMPT", None)
            or os.getenv("FAL_I2I_ENHANCE_PROMPT")
            or "1"
        ).lower() in ("1", "true", "yes", "y", "on")

        self.i2i_safety_tolerance = str(
            getattr(settings, "FAL_I2I_SAFETY_TOLERANCE", None)
            or os.getenv("FAL_I2I_SAFETY_TOLERANCE")
            or "2"
        )

        self.debug = str(
            getattr(settings, "FAL_DEBUG", None)
            or os.getenv("FAL_DEBUG")
            or "0"
        ).lower() in ("1", "true", "yes", "y", "on")

    # ----------------------------
    # Helpers
    # ----------------------------
    @staticmethod
    def _safe_int(v: Any, fallback: int) -> int:
        try:
            return int(v) if v is not None else int(fallback)
        except Exception:
            return int(fallback)

    @staticmethod
    def _extract_first_image(result: Dict[str, Any]) -> Dict[str, Any]:
        images = result.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            return images[0]
        img = result.get("image")
        if isinstance(img, dict):
            return img
        return {}

    @classmethod
    def _normalize_result(cls, result: Dict[str, Any], *, width: int, height: int) -> Dict[str, Any]:
        if not isinstance(result, dict) or not result:
            raise RuntimeError("fal_no_image_returned")

        img0 = cls._extract_first_image(result)
        if not img0:
            raise RuntimeError(f"fal_no_image_returned: keys={list(result.keys())}")

        url = img0.get("url") or img0.get("image_url")
        if not url:
            raise RuntimeError("fal_invalid_image_payload")

        return {
            "url": str(url),
            "width": cls._safe_int(img0.get("width"), int(width)),
            "height": cls._safe_int(img0.get("height"), int(height)),
            "content_type": str(img0.get("content_type") or "image/jpeg") or "image/jpeg",
            "raw": result,
        }

    @staticmethod
    def _require_non_empty(name: str, value: str) -> str:
        v = (value or "").strip()
        if not v:
            raise ValueError(f"missing_{name}")
        return v

    @staticmethod
    def _pos_int(name: str, value: Any, default: int) -> int:
        try:
            n = int(value)
            if n <= 0:
                raise ValueError()
            return n
        except Exception:
            n = int(default)
            if n <= 0:
                raise ValueError(f"invalid_{name}")
            return n

    @staticmethod
    def _clamp01(v: Any, default: float) -> float:
        try:
            x = float(v)
        except Exception:
            x = float(default)
        return max(0.0, min(1.0, x))

    def _clamp_preservation(self, preservation_strength: Any) -> float:
        # LOCKED semantics: preservation_strength is [0..1]
        return self._clamp01(preservation_strength, self.default_preservation_strength)

    @staticmethod
    def _map_preservation_to_transform_strength(ps: float) -> float:
        """
        Map preservation_strength (product semantics) -> provider strength (transform intensity).

        LOCKED:
          ps=1.0 => minimal change
          ps=0.0 => more change

        Provider img2img strength is "how much to change":
          low => minimal change
          high => stronger transformation
        """
        STRENGTH_MIN = 0.05  # allow "almost identical" but not dead-flat
        STRENGTH_MAX = 0.60  # keep within safer band you were already using culturally
        ps = max(0.0, min(1.0, float(ps)))
        return STRENGTH_MIN + (1.0 - ps) * (STRENGTH_MAX - STRENGTH_MIN)

    @staticmethod
    def _map_preservation_to_cfg(cfg_base: float, ps: float) -> float:
        """
        Best-effort control for models without a strength knob (kontext/redux):
          - lower preservation => increase cfg to push prompt edits harder
          - higher preservation => slightly reduce cfg to avoid overwriting identity
        """
        ps = max(0.0, min(1.0, float(ps)))
        # scale in [0.90 .. 1.25]
        scale = 0.90 + (1.0 - ps) * 0.35
        cfg = float(cfg_base) * scale
        # keep reasonable bounds
        return max(3.0, min(12.0, cfg))

    @staticmethod
    def _aspect_ratio_from_wh(width: int, height: int) -> str:
        # kontext/max only accepts these enums:
        # ["21:9","16:9","4:3","3:2","1:1","2:3","3:4","9:16","9:21"]
        if width <= 0 or height <= 0:
            return "1:1"
        if height > width:
            # portrait
            r = height / width
            if r >= 2.1:
                return "9:21"
            return "9:16"
        if width > height:
            # landscape
            r = width / height
            if r >= 2.2:
                return "21:9"
            return "16:9"
        return "1:1"

    def _i2i_kind(self) -> str:
        m = (self.model_i2i or "").lower()
        if "kontext" in m:
            return "kontext"
        if "redux" in m:
            return "redux"
        if "image-to-image" in m or "img2img" in m:
            return "img2img"
        return "unknown"

    @staticmethod
    def _preservation_instruction(ps: float) -> str:
        """
        Add a short instruction that helps endpoints without a 'strength' knob behave more predictably.
        """
        ps = max(0.0, min(1.0, float(ps)))
        if ps >= 0.85:
            return (
                "Preserve the person’s identity strongly: keep facial structure, likeness, age cues, "
                "and overall composition. Apply only the requested edits."
            )
        if ps >= 0.60:
            return (
                "Preserve the person’s identity: keep likeness and facial structure. "
                "Apply the requested edits without changing the subject."
            )
        if ps >= 0.35:
            return (
                "Moderate preservation: keep some likeness cues, but you may adjust styling and details "
                "to satisfy the prompt."
            )
        return (
            "Low preservation: you may significantly change styling, appearance details, and scene to satisfy the prompt."
        )

    def _kontext_prompt(self, prompt: str, negative_prompt: str, ps: float) -> str:
        # kontext doesn’t support negative_prompt; fold into prompt safely, and append preservation instruction.
        instr = self._preservation_instruction(ps)
        if not negative_prompt:
            return f"{prompt}\n\nInstruction: {instr}\n"
        return (
            f"{prompt}\n\n"
            f"Instruction: {instr}\n"
            f"Constraints (do NOT do these): {negative_prompt}\n"
        )

    def _log_debug(self, *, model: str, args: Dict[str, Any]) -> None:
        if not self.debug:
            return
        safe = dict(args)
        if "prompt" in safe and isinstance(safe["prompt"], str) and len(safe["prompt"]) > 500:
            safe["prompt"] = safe["prompt"][:500] + "…"
        logger.info("FAL call", extra={"model": model, "args": safe})

    # ----------------------------
    # Public API
    # ----------------------------
    async def generate_image(
        self,
        *,
        prompt: str,
        negative_prompt: Optional[str] = None,
        seed: int = 0,
        width: int = 1024,
        height: int = 1024,
        num_inference_steps: int = 28,
        guidance_scale: float = 3.5,
    ) -> Dict[str, Any]:
        p = self._require_non_empty("prompt", prompt)
        neg = (negative_prompt or "").strip()
        w = self._pos_int("width", width, 1024)
        h = self._pos_int("height", height, 1024)

        args: Dict[str, Any] = {
            "prompt": p,
            "image_size": {"width": w, "height": h},
            "num_inference_steps": int(num_inference_steps),
            "guidance_scale": float(guidance_scale),
            "num_images": 1,
            "seed": int(seed),
            "enable_safety_checker": False,
            "sync_mode": True,
        }
        if neg:
            args["negative_prompt"] = neg

        self._log_debug(model=self.model_t2i, args=args)

        try:
            result = await fal_client.run_async(self.model_t2i, arguments=args)
            return self._normalize_result(result, width=w, height=h)
        except Exception as e:
            raise RuntimeError(f"fal_t2i_failed: {e}") from e

    async def generate_image_to_image(
        self,
        *,
        prompt: str,
        negative_prompt: Optional[str],
        image_url: str,
        strength: float = 0.75,  # preservation_strength (svc-face semantic, LOCKED)
        seed: Optional[int] = None,
        width: int = 1024,
        height: int = 1024,
        num_inference_steps: int = 40,
        guidance_scale: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Image-to-image (identity lock / editing).

        NOTE:
          - `strength` is DesiFaces preservation_strength in [0..1] (LOCKED semantics).
          - We choose args based on endpoint schema (kontext/redux/img2img).
          - Only the 'img2img' kind supports true 'strength' control. For kontext/redux we do best-effort
            via guidance_scale and preservation instruction in prompt.
        """
        p = self._require_non_empty("prompt", prompt)
        img = self._require_non_empty("image_url", image_url)
        neg = (negative_prompt or "").strip()

        w = self._pos_int("width", width, 1024)
        h = self._pos_int("height", height, 1024)

        preservation = self._clamp_preservation(strength)
        kind = self._i2i_kind()

        # If caller didn’t explicitly provide guidance_scale (>0), use our better I2I default.
        cfg_base = float(guidance_scale) if float(guidance_scale) > 0 else float(self.i2i_guidance_default)

        args: Dict[str, Any] = {}

        if kind == "kontext":
            cfg = self._map_preservation_to_cfg(cfg_base, preservation)
            args = {
                "prompt": self._kontext_prompt(p, neg, preservation),
                "image_url": img,
                "guidance_scale": cfg,
                "enhance_prompt": bool(self.i2i_enhance_prompt_default),
                "aspect_ratio": self._aspect_ratio_from_wh(w, h),
                "num_images": 1,
                "output_format": "jpeg",
                "safety_tolerance": self.i2i_safety_tolerance,
                "sync_mode": True,
            }
            if seed is not None:
                args["seed"] = int(seed)

            logger.info(
                "I2I kontext best-effort: preservation_strength=%.3f guidance_scale=%.3f",
                preservation,
                float(cfg),
            )

        elif kind == "redux":
            cfg = self._map_preservation_to_cfg(cfg_base, preservation)
            prompt2 = f"{p}\n\nInstruction: {self._preservation_instruction(preservation)}\n"
            if neg:
                prompt2 = f"{prompt2}\nAvoid: {neg}\n"

            args = {
                "prompt": prompt2,
                "image_url": img,
                "image_size": {"width": w, "height": h},
                "num_inference_steps": int(num_inference_steps),
                "guidance_scale": cfg,
                "enhance_prompt": bool(self.i2i_enhance_prompt_default),
                "num_images": 1,
                "output_format": "jpeg",
                "sync_mode": True,
            }
            if seed is not None:
                args["seed"] = int(seed)

            logger.info(
                "I2I redux best-effort: preservation_strength=%.3f guidance_scale=%.3f",
                preservation,
                float(cfg),
            )

        else:
            # True img2img strength control (recommended for identity-lock tests)
            transform_strength = self._map_preservation_to_transform_strength(preservation)

            args = {
                "prompt": p,
                "image_url": img,
                "image_size": {"width": w, "height": h},
                "num_inference_steps": int(num_inference_steps),
                "guidance_scale": float(cfg_base),
                "strength": float(transform_strength),
                "num_images": 1,
                "output_format": "jpeg",
                "sync_mode": True,
            }
            if neg:
                args["negative_prompt"] = neg
            if seed is not None:
                args["seed"] = int(seed)

            logger.info(
                "I2I img2img: preservation_strength=%.3f -> transform_strength=%.3f",
                preservation,
                float(transform_strength),
            )

        self._log_debug(model=self.model_i2i, args=args)

        async def _run_once(arguments: Dict[str, Any]) -> Dict[str, Any]:
            result = await fal_client.run_async(self.model_i2i, arguments=arguments)
            return self._normalize_result(result, width=w, height=h)

        # Single retry: fix image_url vs image and drop unknown fields if the model complains.
        try:
            return await _run_once(args)
        except Exception as e:
            msg = str(e).lower()
            retry = dict(args)
            changed = False

            if ("image_url" in msg or "unknown field" in msg or "validation" in msg) and "image_url" in retry:
                retry["image"] = retry.pop("image_url")
                changed = True

            # conservative field drops (varies by endpoint)
            for k in ("negative_prompt", "image_size", "num_inference_steps", "strength", "aspect_ratio", "enhance_prompt"):
                if k in retry and ("unknown field" in msg or "validation" in msg or k in msg):
                    retry.pop(k, None)
                    changed = True

            if changed:
                try:
                    logger.warning("Fal I2I retry due to schema mismatch", extra={"model": self.model_i2i})
                    self._log_debug(model=self.model_i2i, args=retry)
                    return await _run_once(retry)
                except Exception as e2:
                    raise RuntimeError(f"fal_i2i_failed: {e2}") from e2

            raise RuntimeError(f"fal_i2i_failed: {e}") from e

    async def generate_image_img2img(
        self,
        *,
        prompt: str,
        image_url: str,
        negative_prompt: Optional[str] = None,
        seed: Optional[int] = None,
        strength: float = 0.75,
        width: int = 1024,
        height: int = 1024,
        num_inference_steps: int = 40,
        guidance_scale: float = 0.0,
    ) -> Dict[str, Any]:
        return await self.generate_image_to_image(
            prompt=prompt,
            negative_prompt=negative_prompt,
            image_url=image_url,
            strength=strength,
            seed=seed,
            width=width,
            height=height,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
        )