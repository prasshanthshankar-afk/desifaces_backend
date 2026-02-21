from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    try:
        return dict(x)
    except Exception:
        return {}


def _sha256(s: str) -> str:
    import hashlib as _h

    return _h.sha256(s.encode("utf-8")).hexdigest()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _clamp_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except Exception:
        n = default
    return max(lo, min(hi, n))


async def _call_any_async(obj: Any, method_names: List[str], **kwargs: Any) -> Any:
    last_err: Exception | None = None
    for name in method_names:
        fn = getattr(obj, name, None)
        if not fn:
            continue
        try:
            out = fn(**kwargs)
            if hasattr(out, "__await__"):
                return await out
            return out
        except TypeError as e:
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
    if last_err:
        raise last_err
    raise RuntimeError(f"No usable method found on {obj.__class__.__name__}: tried {method_names}")


@dataclass(frozen=True)
class SceneVariantSpec:
    scene: str
    crop: str
    seed: Optional[int] = None


@dataclass(frozen=True)
class SceneGenerateRequest:
    user_id: UUID
    studio_job_id: UUID
    commerce_campaign_id: UUID
    quote_id: UUID
    request_hash: str

    product_assets: Dict[str, Any]  # cutout + mask + metadata (label preservation)
    language: str
    resolution: str
    variants: List[SceneVariantSpec]


@dataclass(frozen=True)
class SceneGenerateResult:
    provider: str
    urls: List[str]
    meta: Dict[str, Any]


class SceneProvider:
    """
    Non-apparel (FMCG/electronics) scene composition provider facade.

    Default behavior:
    - deterministic placeholder URLs for E2E
    - if COMMERCE_ENABLE_REAL_PROVIDERS=1, attempts:
        - Fal scene client
        - internal pipeline client
      then falls back to placeholders.
    """

    def __init__(self) -> None:
        self.enable_real = _env_bool("COMMERCE_ENABLE_REAL_PROVIDERS", default=False)
        self.provider = (os.getenv("COMMERCE_SCENE_PROVIDER") or "fal").strip().lower()
        self.placeholder_base = (os.getenv("COMMERCE_PLACEHOLDER_BASE") or "https://placehold.co").strip().rstrip("/")

    def _placeholder_url(self, *, product_type: str, scene: str, crop: str, idx: int) -> str:
        txt = f"scene+{product_type}+{scene}+{crop}+{idx}"
        return f"{self.placeholder_base}/1024x1024/png?text={txt}"

    def _stable_seed(self, *, request_hash: str, idx: int) -> int:
        h = _sha256(f"{request_hash}:{idx}")
        return int(h[:8], 16) & 0x7FFFFFFF

    async def generate(self, req: SceneGenerateRequest) -> SceneGenerateResult:
        pa = _as_dict(req.product_assets)
        product_type = str(pa.get("product_type") or "mixed").lower()

        urls_fallback: List[str] = []
        for i, v in enumerate(req.variants):
            urls_fallback.append(self._placeholder_url(product_type=product_type, scene=v.scene, crop=v.crop, idx=i))

        if not self.enable_real:
            return SceneGenerateResult(
                provider="placeholder",
                urls=urls_fallback,
                meta={"note": "COMMERCE_ENABLE_REAL_PROVIDERS is off; using placeholders", "variant_count": len(urls_fallback)},
            )

        try:
            if self.provider == "fal":
                from app.services.providers.fal_scene_client import FalSceneClient  # type: ignore

                client = FalSceneClient()
                out = await _call_any_async(client, ["generate", "run", "compose", "execute"], req=req)
                out_d = _as_dict(out)
                urls = out_d.get("urls") if isinstance(out_d.get("urls"), list) else None
                if urls and all(isinstance(x, str) for x in urls):
                    return SceneGenerateResult(provider="fal", urls=list(urls), meta={"raw": out_d})

            if self.provider in ("internal", "native"):
                from app.services.providers.internal_pipeline_client import InternalPipelineClient  # type: ignore

                client = InternalPipelineClient()
                out = await _call_any_async(client, ["generate_scene", "scene", "run_scene", "execute"], req=req)
                out_d = _as_dict(out)
                urls = out_d.get("urls") if isinstance(out_d.get("urls"), list) else None
                if urls and all(isinstance(x, str) for x in urls):
                    return SceneGenerateResult(provider="internal", urls=list(urls), meta={"raw": out_d})

            raise RuntimeError(f"Unknown COMMERCE_SCENE_PROVIDER={self.provider}")

        except Exception as e:  # noqa: BLE001
            logger.exception("SceneProvider.generate failed; falling back to placeholders: %s", e)
            return SceneGenerateResult(
                provider=f"{self.provider}_failed_fallback",
                urls=urls_fallback,
                meta={
                    "error": f"{type(e).__name__}: {e}",
                    "note": "real provider failed; using placeholders",
                    "variant_count": len(urls_fallback),
                },
            )

    @staticmethod
    def build_variants_from_request(*, quote_request: Dict[str, Any], request_hash: str) -> List[SceneVariantSpec]:
        """
        Deterministic variant planner for FMCG/electronics:
        - studio white packshot (required)
        - lifestyle scenes (vanity/home/desk)
        - detail macros + in-hand/usage
        """
        qr = _as_dict(quote_request)
        out = _as_dict(qr.get("outputs"))
        num_images = out.get("num_images") or qr.get("count") or 12
        n = _clamp_int(num_images, default=12, lo=1, hi=20)

        scenes = [
            "studio_white_packshot",
            "lifestyle_vanity",
            "lifestyle_home",
            "lifestyle_desk",
            "lifestyle_outdoor",
            "in_hand",
            "usage_demo",
            "detail_macro",
        ]
        crops = ["hero", "closeup", "square", "vertical_safe"]

        variants: List[SceneVariantSpec] = []
        for i in range(n):
            scene = scenes[i % len(scenes)]
            crop = crops[(i // len(scenes)) % len(crops)]
            seed = int(int(hashlib.sha256(f"{request_hash}:{i}".encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF)
            variants.append(SceneVariantSpec(scene=scene, crop=crop, seed=seed))

        return variants