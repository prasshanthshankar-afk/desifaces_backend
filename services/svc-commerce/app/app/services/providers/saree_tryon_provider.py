# services/svc-commerce/app/app/services/providers/saree_tryon_provider.py
from __future__ import annotations

import inspect
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.services.providers.saree_drape_provider import (  # reuse proven helpers
    _as_dict,
    _as_list,
    _env_bool,
    _env_int,
    _env_str,
    _extract_items,
    _extract_model_url,
    _pick_item,
    _is_jewelry_item,
    _item_url,
    _download_to_path,
    _ext_from_url,
    _get_image_size,
    _qc_png_not_blank_or_black,
    _sanitize_for_path,
    _stable_seed,
    _guess_content_type,
    _safe_filename,
    _call_any_upload,
    _write_json,
)

logger = logging.getLogger(__name__)


@dataclass
class SareeTryOnConfig:
    enabled: bool = False
    strict: bool = False

    require_full_body: bool = True
    drape_style_default: str = "nivi"

    # if strict, do not fall back to overlay provider
    allow_overlay_fallback: bool = True

    download_timeout_s: int = 60
    download_max_mb: int = 30

    qc_enabled: bool = True
    qc_black_thresh: int = 10
    qc_low_dynamic_eps: int = 3

    out_container_prefix: str = "commerce/vton/saree_tryon"
    require_storage_url: bool = True

    run_dir_base: str = "/tmp/df_saree_tryon_runs"
    keep_run_dir: bool = False

    @staticmethod
    def from_env() -> "SareeTryOnConfig":
        strict = _env_bool("COMMERCE_SAREE_STRICT", False) or _env_bool("DF_SAREE_STRICT", False)
        enabled = _env_bool("COMMERCE_ENABLE_SAREE_TRYON_PROVIDER", False) or _env_bool("DF_ENABLE_SAREE_TRYON_PROVIDER", False)
        if strict:
            enabled = True

        allow_overlay_fallback = _env_bool("DF_SAREE_TRYON_ALLOW_OVERLAY_FALLBACK", True)
        if strict:
            allow_overlay_fallback = False

        return SareeTryOnConfig(
            enabled=enabled,
            strict=strict,
            require_full_body=_env_bool("DF_SAREE_TRYON_REQUIRE_FULL_BODY", True),
            drape_style_default=(_env_str("DF_SAREE_TRYON_DRAPE_STYLE_DEFAULT", "nivi") or "nivi").lower(),
            allow_overlay_fallback=allow_overlay_fallback,
            download_timeout_s=_env_int("DF_SAREE_DOWNLOAD_TIMEOUT_S", 60),
            download_max_mb=_env_int("DF_SAREE_DOWNLOAD_MAX_MB", 30),
            qc_enabled=_env_bool("DF_SAREE_QC_ENABLED", True),
            qc_black_thresh=_env_int("DF_SAREE_QC_BLACK_THRESH", 10),
            qc_low_dynamic_eps=_env_int("DF_SAREE_QC_LOW_DYN_EPS", 3),
            out_container_prefix=_env_str("DF_SAREE_TRYON_OUT_PREFIX", "commerce/vton/saree_tryon").strip().strip("/"),
            require_storage_url=_env_bool("DF_SAREE_DRAPE_REQUIRE_STORAGE_URL", True),
            run_dir_base=_env_str("DF_SAREE_TRYON_RUN_DIR_BASE", "/tmp/df_saree_tryon_runs"),
            keep_run_dir=_env_bool("DF_SAREE_TRYON_KEEP_RUN_DIR", False),
        )


class SareeTryOnProvider:
    """
    ML-first provider (fal.ai / fine-tuned model / OpenAI edit wrapper).
    This is the provider that should produce actual drape (pleats + pallu), not a 2D overlay.
    """

    def __init__(
        self,
        *,
        storage: Any = None,
        tryon_client: Any = None,
        saree_refiner: Any = None,
        config: Optional[SareeTryOnConfig] = None,
    ) -> None:
        self.storage = storage
        self.tryon_client = tryon_client
        self.saree_refiner = saree_refiner
        self.cfg = config or SareeTryOnConfig.from_env()

    def can_handle(self, *, request: Dict[str, Any], resolved_inputs: Dict[str, Any]) -> bool:
        if not self.cfg.enabled:
            return False
        ri = resolved_inputs or {}
        outfit_kind = (ri.get("outfit_kind") or ri.get("outfit") or "").strip().lower()
        if outfit_kind == "saree_set":
            return True
        if bool(ri.get("saree_like")):
            return True
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
        debug: Dict[str, Any] = {"provider": "saree_tryon", "steps": [], "strict": self.cfg.strict, "qc": {}}

        if not self.can_handle(request=request, resolved_inputs=ri):
            raise ValueError("SAREE_TRYON_NOT_APPLICABLE")

        if self.cfg.require_storage_url and not self.storage:
            raise RuntimeError("SAREE_TRYON_STORAGE_MISSING")

        model_url = _extract_model_url(request, ri)
        if not model_url:
            raise ValueError("SAREE_TRYON_MISSING_MODEL_URL")
        debug["model_url"] = model_url

        full_body = bool(
            _as_dict(ri.get("views")).get("full_body")
            or _as_dict(_as_dict(request.get("input")).get("views")).get("full_body")
        )
        if self.cfg.require_full_body and not full_body:
            raise ValueError("SAREE_TRYON_REQUIRES_FULL_BODY")

        items = _extract_items(request, ri)
        if not items:
            raise ValueError("SAREE_TRYON_MISSING_ITEMS")
        debug["num_items"] = len(items)

        saree_it = _pick_item(items, "saree") or _pick_item(items, "sari")
        blouse_it = _pick_item(items, "blouse") or _pick_item(items, "choli")
        jewelry_items = [it for it in items if _is_jewelry_item(it)]

        saree_url = _item_url(saree_it or {})
        blouse_url = _item_url(blouse_it or {})
        jewelry_urls = [u for u in (_item_url(j) for j in jewelry_items) if u]

        if not saree_url:
            raise ValueError("SAREE_TRYON_MISSING_SAREE_URL")

        drape_style = (ri.get("drape_style") or ri.get("drape") or self.cfg.drape_style_default or "nivi").strip().lower()
        debug["drape_style"] = drape_style
        debug["saree_url"] = saree_url
        debug["blouse_url"] = blouse_url
        debug["jewelry_urls"] = jewelry_urls

        seed = _stable_seed(str(job_id), str(user_id), saree_url, blouse_url or "", drape_style)
        debug["seed"] = seed

        os.makedirs(self.cfg.run_dir_base, exist_ok=True)
        run_dir = tempfile.mkdtemp(prefix=f"df_saree_tryon_{_sanitize_for_path(job_id)}_", dir=self.cfg.run_dir_base)
        debug["run_dir"] = run_dir

        keep_dir = bool(self.cfg.keep_run_dir)

        try:
            max_bytes = int(self.cfg.download_max_mb) * 1024 * 1024

            model_path = os.path.join(run_dir, f"model{_ext_from_url(model_url, '.png')}")
            saree_path = os.path.join(run_dir, f"saree{_ext_from_url(saree_url, '.png')}")
            blouse_path = os.path.join(run_dir, f"blouse{_ext_from_url(blouse_url, '.png')}") if blouse_url else ""
            out_path = os.path.join(run_dir, "tryon.png")

            debug["steps"].append("download_inputs")
            _download_to_path(model_url, model_path, timeout_s=self.cfg.download_timeout_s, max_bytes=max_bytes)
            _download_to_path(saree_url, saree_path, timeout_s=self.cfg.download_timeout_s, max_bytes=max_bytes)
            if blouse_url:
                _download_to_path(blouse_url, blouse_path, timeout_s=self.cfg.download_timeout_s, max_bytes=max_bytes)

            try:
                model_w, model_h = _get_image_size(model_path)
                debug["model_size"] = [int(model_w), int(model_h)]
            except Exception as e:
                debug["model_size_warn"] = f"{type(e).__name__}: {e}"

            if self.cfg.qc_enabled:
                debug["qc"]["model"] = _qc_png_not_blank_or_black(
                    model_path,
                    black_thresh=max(2, int(self.cfg.qc_black_thresh)),
                    low_dyn_eps=int(self.cfg.qc_low_dynamic_eps),
                )

            prompt = self._build_tryon_prompt(
                drape_style=drape_style,
                has_blouse=bool(blouse_url),
                jewelry_urls=jewelry_urls,
            )

            debug["steps"].append("ml_tryon")
            out_path2 = self._call_tryon_any(
                person_image_path=model_path,
                saree_image_path=saree_path,
                blouse_image_path=blouse_path if blouse_url else "",
                prompt=prompt,
                seed=seed,
                out_path=out_path,
                debug=debug,
            )
            if not out_path2 or not os.path.exists(out_path2) or os.path.getsize(out_path2) == 0:
                keep_dir = True
                raise RuntimeError("SAREE_TRYON_NO_OUTPUT")

            if self.cfg.qc_enabled:
                debug["qc"]["tryon"] = _qc_png_not_blank_or_black(
                    out_path2,
                    black_thresh=int(self.cfg.qc_black_thresh),
                    low_dyn_eps=int(self.cfg.qc_low_dynamic_eps),
                )

            debug["steps"].append("upload_output")
            output_url = self._upload_output(job_id=job_id, user_id=user_id, local_path=out_path2, debug=debug)

            debug["steps"].append("write_debug_json")
            _write_json(os.path.join(run_dir, "debug.json"), debug)

            return {
                "provider": "saree_tryon",
                "provider_mode": "ml_tryon",
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
                    "jewelry_urls": jewelry_urls,
                    "provider_selected": "saree_tryon",
                    "provider_mode": "ml_tryon",
                    "model_size": debug.get("model_size"),
                },
                "debug": debug,
            }

        except Exception as e:
            keep_dir = True
            debug["error"] = f"{type(e).__name__}: {e}"
            try:
                _write_json(os.path.join(run_dir, "debug.json"), debug)
            except Exception:
                pass
            raise
        finally:
            if not keep_dir:
                try:
                    shutil.rmtree(run_dir, ignore_errors=True)
                except Exception:
                    pass

    def _build_tryon_prompt(self, *, drape_style: str, has_blouse: bool, jewelry_urls: List[Any]) -> str:
        blouse_txt = "matching blouse" if has_blouse else "traditional blouse"
        jewelry_txt = "with traditional Indian jewelry (earrings, necklace, bangles)" if jewelry_urls else ""
        style_txt = (
            "Nivi style saree drape with pleats at the waist and pallu over the shoulder"
            if drape_style == "nivi"
            else f"{drape_style} style saree drape"
        )
        return (
            "Photorealistic full-body fashion photo. "
            "Convert the outfit into a REAL Indian saree drape (NOT a dress, NOT a gown, NOT a flat overlay). "
            f"Ensure correct saree draping: {style_txt}. "
            f"Include a {blouse_txt}. {jewelry_txt}. "
            "The saree fabric pattern should resemble the provided saree image. "
            "Preserve the person’s identity, face, skin tone, pose, hands, and body proportions. "
            "Natural cloth folds, pleats, realistic pallu fall, correct waist pleats, no pasted edges, no artifacts."
        )

    def _call_tryon_any(
        self,
        *,
        person_image_path: str,
        saree_image_path: str,
        blouse_image_path: str,
        prompt: str,
        seed: int,
        out_path: str,
        debug: Dict[str, Any],
    ) -> str:
        client = self.tryon_client or self.saree_refiner
        if client is None:
            raise RuntimeError("SAREE_TRYON_NO_CLIENT: wire tryon_client or saree_refiner into SareeTryOnProvider")

        # best-effort “any client” call with signature filtering
        candidates = ["tryon", "generate_tryon", "generate", "edit", "refine"]
        last_err: Optional[Exception] = None

        for name in candidates:
            m = getattr(client, name, None)
            if not callable(m):
                continue

            try:
                sig = None
                try:
                    sig = inspect.signature(m)
                except Exception:
                    sig = None

                kwargs: Dict[str, Any] = {
                    "person_image_path": person_image_path,
                    "image_path": person_image_path,
                    "base_image_path": person_image_path,
                    "garment_image_path": saree_image_path,
                    "saree_image_path": saree_image_path,
                    "ref_image_path": saree_image_path,
                    "reference_image_path": saree_image_path,
                    "blouse_image_path": blouse_image_path,
                    "prompt": prompt,
                    "seed": seed,
                    "out_path": out_path,
                    "output_path": out_path,
                    "debug": debug,
                }

                if sig is not None:
                    filtered: Dict[str, Any] = {}
                    for k, v in kwargs.items():
                        if k in sig.parameters and v not in ("", None):
                            filtered[k] = v
                    kwargs = filtered

                debug["ml_method_used"] = name
                res = m(**kwargs)

                # normalize result → local path
                if isinstance(res, str) and os.path.exists(res):
                    return res
                if isinstance(res, str) and res.strip().startswith(("http://", "https://")):
                    # if a URL is returned, download it to out_path
                    from urllib.request import urlretrieve
                    urlretrieve(res, out_path)
                    return out_path
                if isinstance(res, dict):
                    for k in ("path", "image_path", "output_path", "local_path"):
                        v = res.get(k)
                        if isinstance(v, str) and os.path.exists(v):
                            return v
                    for k in ("url", "image_url", "output_url"):
                        v = res.get(k)
                        if isinstance(v, str) and v.startswith(("http://", "https://")):
                            from urllib.request import urlretrieve
                            urlretrieve(v, out_path)
                            return out_path

                # unknown return type: treat as failure
                raise RuntimeError(f"SAREE_TRYON_BAD_RETURN[{name}]: {type(res).__name__}")

            except Exception as e:
                last_err = e
                debug[f"ml_attempt_fail_{name}"] = f"{type(e).__name__}: {e}"
                continue

        raise RuntimeError(f"SAREE_TRYON_ALL_METHODS_FAILED: {last_err!r}")

    def _upload_output(self, *, job_id: Any, user_id: Any, local_path: str, debug: Dict[str, Any]) -> str:
        if not self.storage:
            if self.cfg.require_storage_url:
                raise RuntimeError("SAREE_TRYON_STORAGE_MISSING")
            debug["upload"] = "no_storage_service"
            return local_path

        content_type = _guess_content_type(local_path)
        blob_name = f"{self.cfg.out_container_prefix}/{user_id}/{job_id}/{uuid4().hex}/{_safe_filename('.png')}"
        url = _call_any_upload(self.storage, local_path, blob_name, content_type)
        debug["upload"] = {"blob": blob_name}
        return url