from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import requests

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


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _stable_seed(*parts: str) -> int:
    h = _sha256("|".join([p or "" for p in parts]))
    return int(h[:8], 16)


def _guess_content_type(path: str) -> str:
    p = path.lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    if p.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def _safe_filename(ext: str = ".png") -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{uuid4().hex}{ext}"


def _download_to_path(url: str, out_path: str, timeout_s: int = 60) -> None:
    with requests.get(url, stream=True, timeout=timeout_s) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def _is_http_url(x: str) -> bool:
    return isinstance(x, str) and (x.startswith("http://") or x.startswith("https://"))


def _extract_items(req: Dict[str, Any], resolved_inputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Try multiple known shapes to be resilient across refactors.
    candidates: List[Any] = []
    candidates.append(resolved_inputs.get("items"))
    candidates.append(_as_dict(resolved_inputs.get("product_assets")).get("items"))
    candidates.append(req.get("items"))
    candidates.append(_as_dict(req.get("product_assets")).get("items"))
    candidates.append(_as_dict(_as_dict(req.get("input")).get("product_assets")).get("items"))
    for c in candidates:
        items = _as_list(c)
        if items and isinstance(items[0], dict):
            return [ _as_dict(i) for i in items ]
    return []


def _extract_model_url(req: Dict[str, Any], resolved_inputs: Dict[str, Any]) -> Optional[str]:
    # Model ref may be under request.model_ref or input.model_ref or resolved_inputs.model_ref
    for blob in (
        resolved_inputs.get("model_ref"),
        req.get("model_ref"),
        _as_dict(req.get("input")).get("model_ref"),
        _as_dict(resolved_inputs.get("views")).get("model_ref"),
    ):
        d = _as_dict(blob)
        for k in ("url", "image_url", "full_body_url", "ref_url"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v
    # Sometimes provider puts it directly
    for k in ("model_url", "person_url", "full_body_model_url"):
        v = resolved_inputs.get(k) or req.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _pick_item(items: List[Dict[str, Any]], want: str) -> Optional[Dict[str, Any]]:
    want = (want or "").strip().lower()

    def score(it: Dict[str, Any]) -> int:
        kind = str(it.get("kind") or it.get("type") or it.get("component") or "").lower()
        name = str(it.get("name") or "").lower()
        cat = str(it.get("category") or "").lower()
        s = 0
        if want in kind:
            s += 5
        if want in cat:
            s += 3
        if want in name:
            s += 2
        # common saree/blouse signals
        if want == "saree" and ("sari" in kind or "saree" in kind):
            s += 5
        if want == "blouse" and ("blouse" in kind or "choli" in kind):
            s += 5
        if want == "jewelry" and (kind.startswith("jewelry") or "jewel" in kind or "accessory" in cat):
            s += 5
        return s

    best: Optional[Dict[str, Any]] = None
    best_s = -1
    for it in items:
        s = score(it)
        if s > best_s:
            best_s = s
            best = it
    if best_s <= 0:
        return None
    return best


def _item_url(it: Dict[str, Any]) -> Optional[str]:
    for k in ("url", "image_url", "asset_url", "src"):
        v = it.get(k)
        if isinstance(v, str) and v:
            return v
    # Some shapes embed {asset:{url:...}}
    asset = _as_dict(it.get("asset"))
    for k in ("url", "image_url"):
        v = asset.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _try_pil_compose(base_path: str, overlay_path: str, out_path: str) -> bool:
    # best-effort RGBA alpha composite
    try:
        from PIL import Image  # pillow
    except Exception:
        logger.warning("Pillow not installed; skipping local compose.")
        return False

    try:
        base = Image.open(base_path).convert("RGBA")
        ov = Image.open(overlay_path).convert("RGBA")
        # resize overlay to base for now (templates should match; this is a safe MVP)
        if ov.size != base.size:
            ov = ov.resize(base.size)
        comp = Image.alpha_composite(base, ov)
        comp.save(out_path, "PNG")
        return True
    except Exception as e:
        logger.warning("PIL compose failed: %s", e)
        return False


@dataclass
class SareeDrapeConfig:
    enabled: bool = True
    prefer_over_fashn: bool = True
    require_full_body: bool = True

    drape_style_default: str = "nivi"
    enable_blender: bool = False
    template_nivi_blend: str = ""  # e.g. /app/app/services/drape/blender_scripts/templates/nivi.blend

    # Refinement
    enable_refine: bool = True
    refine_strength: float = 0.55  # used if provider supports
    refine_steps: int = 28

    # Upload paths
    out_container_prefix: str = "commerce/vton/saree_drape"

    @staticmethod
    def from_env() -> "SareeDrapeConfig":
        return SareeDrapeConfig(
            enabled=_env_bool("DF_ENABLE_SAREE_DRAPE_PROVIDER", True),
            prefer_over_fashn=_env_bool("DF_SAREE_DRAPE_PREFER", True),
            require_full_body=_env_bool("DF_SAREE_DRAPE_REQUIRE_FULL_BODY", True),
            drape_style_default=(os.getenv("DF_SAREE_DRAPE_STYLE_DEFAULT") or "nivi").strip().lower(),
            enable_blender=_env_bool("DF_SAREE_DRAPE_ENABLE_BLENDER", False),
            template_nivi_blend=(os.getenv("DF_SAREE_TEMPLATE_NIVI_BLEND") or "").strip(),
            enable_refine=_env_bool("DF_SAREE_DRAPE_ENABLE_REFINE", True),
            refine_strength=float(os.getenv("DF_SAREE_REFINE_STRENGTH") or "0.55"),
            refine_steps=int(float(os.getenv("DF_SAREE_REFINE_STEPS") or "28")),
            out_container_prefix=(os.getenv("DF_SAREE_DRAPE_OUT_PREFIX") or "commerce/vton/saree_drape").strip().strip("/"),
        )


class SareeDrapeProvider:
    """
    MVP (Option 1): 2.5D template drape + (optional) 2D refine + jewelry hints.

    - If Blender/templates aren't available, we *gracefully fail* so caller can fallback to FASHN.
    - Keeps resolved_inputs rich so you can debug why it fell back.
    """

    def __init__(
        self,
        *,
        storage: Any = None,  # AzureStorageService (optional)
        blender_runner: Any = None,  # BlenderRunner (optional)
        saree_refiner: Any = None,  # SareeRefiner (optional)
        config: Optional[SareeDrapeConfig] = None,
    ) -> None:
        self.storage = storage
        self.blender_runner = blender_runner
        self.saree_refiner = saree_refiner
        self.cfg = config or SareeDrapeConfig.from_env()

    def can_handle(self, *, request: Dict[str, Any], resolved_inputs: Dict[str, Any]) -> bool:
        if not self.cfg.enabled:
            return False
        ri = resolved_inputs or {}
        outfit_kind = (ri.get("outfit_kind") or ri.get("outfit") or "").strip().lower()
        if outfit_kind == "saree_set":
            return True
        # alternate flag
        if bool(ri.get("saree_like")):
            return True
        # request hint
        rk = (_as_dict(request.get("input")).get("outfit_kind") or request.get("outfit_kind") or "").strip().lower()
        return rk == "saree_set"

    def run(
        self,
        *,
        job_id: Any,
        user_id: Any,
        request: Dict[str, Any],
        resolved_inputs: Dict[str, Any],
    ) -> Dict[str, Any]:
        ri = _as_dict(resolved_inputs)
        debug: Dict[str, Any] = {"provider": "saree_drape", "steps": []}

        if not self.can_handle(request=request, resolved_inputs=ri):
            raise ValueError("SareeDrapeProvider cannot handle this request")

        model_url = _extract_model_url(request, ri)
        if not model_url:
            raise ValueError("missing model_ref url for saree_drape")
        debug["model_url"] = model_url

        # Full-body gate (so we don't generate nonsense)
        full_body = bool(_as_dict(ri.get("views")).get("full_body") or _as_dict(_as_dict(request.get("input")).get("views")).get("full_body"))
        if self.cfg.require_full_body and not full_body:
            raise ValueError("saree_drape requires full_body model_ref (views.full_body=true)")

        items = _extract_items(request, ri)
        if not items:
            raise ValueError("missing product_assets.items for saree_drape")
        debug["num_items"] = len(items)

        saree_it = _pick_item(items, "saree") or _pick_item(items, "sari")
        blouse_it = _pick_item(items, "blouse") or _pick_item(items, "choli")
        jewelry_items = [it for it in items if _pick_item([it], "jewelry") is not None]

        saree_url = _item_url(saree_it or {})
        blouse_url = _item_url(blouse_it or {})
        if not saree_url:
            raise ValueError("missing saree item url for saree_drape")

        drape_style = (ri.get("drape_style") or ri.get("drape") or self.cfg.drape_style_default or "nivi").strip().lower()
        debug["drape_style"] = drape_style
        debug["saree_url"] = saree_url
        debug["blouse_url"] = blouse_url
        debug["jewelry_urls"] = [_item_url(j) for j in jewelry_items if _item_url(j)]

        seed = _stable_seed(str(job_id), str(user_id), saree_url, blouse_url or "", drape_style)
        debug["seed"] = seed

        with tempfile.TemporaryDirectory(prefix="df_saree_drape_") as td:
            model_path = os.path.join(td, "model.png")
            saree_path = os.path.join(td, "saree.png")
            blouse_path = os.path.join(td, "blouse.png")
            overlay_path = os.path.join(td, "overlay.png")
            composed_path = os.path.join(td, "composed.png")
            refined_path = os.path.join(td, "refined.png")

            debug["steps"].append("download_inputs")
            _download_to_path(model_url, model_path)
            _download_to_path(saree_url, saree_path)
            if blouse_url:
                try:
                    _download_to_path(blouse_url, blouse_path)
                except Exception:
                    blouse_path = ""

            # Step A: 2.5D drape via Blender template (preferred)
            used_blender = False
            if self.cfg.enable_blender:
                debug["steps"].append("blender_drape")
                overlay_ok = self._run_blender_drape(
                    drape_style=drape_style,
                    saree_texture_path=saree_path,
                    out_overlay_path=overlay_path,
                    debug=debug,
                )
                used_blender = overlay_ok

            # If no Blender overlay, we FAIL FAST so caller can fallback to FASHN
            if self.cfg.enable_blender and not used_blender:
                raise RuntimeError("blender_drape failed (no overlay produced)")

            # Step B: local compose (model + overlay)
            if used_blender:
                debug["steps"].append("compose_overlay")
                ok = _try_pil_compose(model_path, overlay_path, composed_path)
                if not ok:
                    # if local compose fails, still let refiner do the heavy lifting using model only (but that’s weaker)
                    composed_path = model_path
                    debug["compose_used"] = "skipped"
                else:
                    debug["compose_used"] = "pil_alpha_composite"
            else:
                composed_path = model_path
                debug["compose_used"] = "none"

            # Step C: 2D refine (optional but strongly recommended)
            final_local_path = composed_path
            if self.cfg.enable_refine and self.saree_refiner is not None:
                debug["steps"].append("refine_2d")
                prompt = self._build_refine_prompt(
                    drape_style=drape_style,
                    has_blouse=bool(blouse_url),
                    jewelry_urls=debug["jewelry_urls"],
                )
                out_path = self.saree_refiner.refine(
                    image_path=final_local_path,
                    prompt=prompt,
                    seed=seed,
                    steps=self.cfg.refine_steps,
                    strength=self.cfg.refine_strength,
                    debug=debug,
                )
                if out_path and os.path.exists(out_path):
                    final_local_path = out_path
                    debug["refine_used"] = True
                else:
                    debug["refine_used"] = False

            # Step D: Upload final
            debug["steps"].append("upload_output")
            output_url = self._upload_output(
                job_id=job_id,
                user_id=user_id,
                local_path=final_local_path,
                debug=debug,
            )

        # Return shape is intentionally dict-based to be compatible with your existing pipeline
        # (vton_provider can merge this into computed/outputs).
        result: Dict[str, Any] = {
            "provider": "saree_drape",
            "status": "succeeded",
            "output_url": output_url,
            "seed": seed,
            "resolved_inputs": {
                **ri,
                "outfit_kind": "saree_set",
                "saree_like": True,
                "drape_style": drape_style,
                "saree_url": saree_url,
                "blouse_url": blouse_url,
                "jewelry_urls": debug.get("jewelry_urls", []),
                "provider_selected": "saree_drape",
            },
            "debug": debug,
        }
        return result

    def _build_refine_prompt(self, *, drape_style: str, has_blouse: bool, jewelry_urls: List[Any]) -> str:
        # IMPORTANT: this is where we fight the “dress/top” failure mode.
        # We explicitly enforce saree drape geometry + pallu + pleats + blouse.
        blouse_txt = "matching blouse" if has_blouse else "traditional blouse"
        jewelry_txt = "with traditional Indian jewelry (earrings, necklace, bangles)" if jewelry_urls else ""
        style_txt = "Nivi style saree drape with pleats at the waist and pallu over the shoulder" if drape_style == "nivi" else f"{drape_style} style saree drape"
        return (
            "Photorealistic full-body fashion photo. "
            "Subject is wearing an Indian saree (NOT a dress, NOT a gown, NOT a two-piece western outfit). "
            f"Ensure correct saree draping: {style_txt}. "
            f"Include a {blouse_txt}. "
            f"{jewelry_txt}. "
            "Preserve the person’s body, pose, face, hands, and identity. "
            "Natural cloth folds, silk/cotton texture, realistic lighting, no artifacts."
        )

    def _run_blender_drape(
        self,
        *,
        drape_style: str,
        saree_texture_path: str,
        out_overlay_path: str,
        debug: Dict[str, Any],
    ) -> bool:
        if self.blender_runner is None:
            debug["blender"] = "missing_blender_runner"
            return False

        template_blend = ""
        if drape_style == "nivi":
            template_blend = self.cfg.template_nivi_blend

        if not template_blend:
            debug["blender"] = {"error": "missing template blend for style", "style": drape_style}
            return False

        try:
            ok = bool(
                self.blender_runner.render_saree_overlay(
                    template_blend=template_blend,
                    saree_texture_path=saree_texture_path,
                    out_png_path=out_overlay_path,
                )
            )
            debug["blender"] = {"used": ok, "template": template_blend}
            return ok and os.path.exists(out_overlay_path)
        except Exception as e:
            debug["blender"] = {"error": str(e), "template": template_blend}
            return False

    def _upload_output(self, *, job_id: Any, user_id: Any, local_path: str, debug: Dict[str, Any]) -> str:
        if not self.storage:
            # fallback: return local path (caller can upload); but in svc-commerce you likely want a URL
            debug["upload"] = "no_storage_service"
            return local_path

        content_type = _guess_content_type(local_path)
        blob_name = f"{self.cfg.out_container_prefix}/{user_id}/{job_id}/{uuid4().hex}/{_safe_filename('.png')}"

        # Be resilient to AzureStorageService method naming variations
        for method_name in ("upload_file", "upload_path", "upload_local_file", "upload"):
            m = getattr(self.storage, method_name, None)
            if callable(m):
                try:
                    url = m(local_path, blob_name, content_type=content_type)  # type: ignore[misc]
                    if isinstance(url, str) and url:
                        debug["upload"] = {"method": method_name, "blob": blob_name}
                        return url
                except TypeError:
                    # some variants don't accept content_type
                    url = m(local_path, blob_name)  # type: ignore[misc]
                    if isinstance(url, str) and url:
                        debug["upload"] = {"method": method_name, "blob": blob_name}
                        return url

        raise RuntimeError("AzureStorageService has no supported upload method")