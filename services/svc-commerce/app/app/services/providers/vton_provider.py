from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
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


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    # important: support "single item" dicts
    return [x]


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _clamp_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except Exception:
        n = default
    return max(lo, min(hi, n))


def _coerce_float(v: Any, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        try:
            return float(str(v))
        except Exception:
            return default


def _first_http_str(x: Any) -> Optional[str]:
    if isinstance(x, str) and x.strip().startswith("http"):
        return x.strip()
    if isinstance(x, list):
        for it in x:
            u = _first_http_str(it)
            if u:
                return u
    if isinstance(x, dict):
        for k in ("url", "image_url", "src"):
            u = x.get(k)
            if isinstance(u, str) and u.strip().startswith("http"):
                return u.strip()
    return None


def _http_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    data: Optional[Dict[str, Any]] = None,
    timeout_s: int = 120,
) -> Dict[str, Any]:
    """
    Safe JSON HTTP helper for fal queue endpoints.

    IMPORTANT:
      - For GET/HEAD: DO NOT send a body at all.
      - Always pass Request(method=...) explicitly.
    """
    m = (method or "GET").strip().upper()

    hdrs: Dict[str, str] = {"Accept": "application/json"}
    hdrs.update(headers or {})

    body: Optional[bytes] = None
    if m not in ("GET", "HEAD") and data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = Request(url=url, method=m, headers=hdrs, data=body)

    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read() or b""
            txt = raw.decode("utf-8", errors="replace").strip()
            if not txt:
                return {}
            try:
                out = json.loads(txt)
            except Exception:
                return {"raw": txt}
            return out if isinstance(out, dict) else {"raw": out}

    except HTTPError as e:
        raw = b""
        try:
            raw = e.read() or b""
        except Exception:
            pass
        txt = raw.decode("utf-8", errors="replace").strip() if raw else str(e)
        try:
            j = json.loads(txt) if txt else {}
        except Exception:
            j = {"raw": txt}
        raise RuntimeError(f"Fal HTTPError code={e.code} url={url} body={j}") from e

    except URLError as e:
        raise RuntimeError(f"Fal URLError url={url} err={e}") from e


@dataclass(frozen=True)
class VTONVariantSpec:
    pose: str
    background: str
    drape_style: Optional[str] = None
    seed: Optional[int] = None


@dataclass(frozen=True)
class VTONGenerateRequest:
    user_id: UUID
    studio_job_id: UUID
    commerce_campaign_id: UUID
    quote_id: UUID
    request_hash: str

    product_assets: Dict[str, Any]
    model_ref: Dict[str, Any]
    language: str
    resolution: str
    variants: List[VTONVariantSpec]


@dataclass(frozen=True)
class VTONGenerateResult:
    provider: str
    urls: List[str]
    meta: Dict[str, Any]


class VTONProvider:
    """
    Provider routing:
      - Saree-like:
          (Phase-3) SareeDrapeProvider (internal saree-first pipeline) if enabled
          else FASHN Try-On (fal-ai/fashn/tryon/v1.5+)
          else Leffa (legacy) if explicitly selected
      - Non-saree: Leffa virtual-tryon (legacy default)

    Env (legacy commerce prefix kept):
      - COMMERCE_ENABLE_REAL_PROVIDERS=1
      - COMMERCE_VTON_PROVIDER=fal
      - COMMERCE_ENABLE_SAREE_DRAPE_PROVIDER=0|1   (alias: DF_ENABLE_SAREE_DRAPE_PROVIDER)
      - COMMERCE_ENFORCE_FULL_BODY_FOR_SAREE=0|1

      - Leffa:
          COMMERCE_FAL_ENDPOINT_ID=fal-ai/leffa/virtual-tryon
      - FASHN:
          COMMERCE_FAL_FASHN_ENDPOINT_ID=fal-ai/fashn/tryon/v1.5
          COMMERCE_SAREE_PROVIDER=fashn|leffa   (default fashn)
          COMMERCE_FASHN_MODE=performance|balanced|quality (default quality)
          COMMERCE_FASHN_CATEGORY_SAREE=tops|bottoms|one-pieces|auto (default one-pieces)
          COMMERCE_FASHN_GARMENT_PHOTO_TYPE=auto|model|flat-lay (default auto)
          COMMERCE_FASHN_MODERATION_LEVEL=none|permissive|conservative (default permissive)
          COMMERCE_FASHN_SEGMENTATION_FREE=1|0 (default 1)
          COMMERCE_FASHN_OUTPUT_FORMAT=png|jpeg (default png)
          COMMERCE_FASHN_SYNC_MODE=1|0 (default 0)

      - Fal key:
          FAL_KEY or FAL_API_KEY or COMMERCE_FAL_KEY
    """

    def __init__(self) -> None:
        self.enable_real = _env_bool("COMMERCE_ENABLE_REAL_PROVIDERS", default=False)
        self.provider = (_env_str("COMMERCE_VTON_PROVIDER", "fal") or "fal").strip().lower()

        # allow both env namespaces
        self.enable_saree_drape_provider = (
            _env_bool("COMMERCE_ENABLE_SAREE_DRAPE_PROVIDER", default=False)
            or _env_bool("DF_ENABLE_SAREE_DRAPE_PROVIDER", default=False)
        )
        self.enforce_full_body_for_saree = _env_bool("COMMERCE_ENFORCE_FULL_BODY_FOR_SAREE", default=False)

        self.placeholder_base = (_env_str("COMMERCE_PLACEHOLDER_BASE", "https://placehold.co") or "https://placehold.co").rstrip("/")
        self.max_provider_images = _clamp_int(_env_str("COMMERCE_MAX_PROVIDER_IMAGES", "4"), default=4, lo=1, hi=24)

        self.demo_mode = _env_bool("COMMERCE_DEMO_MODE", default=False)
        self.allow_placeholder_fallback = _env_bool("COMMERCE_ALLOW_PLACEHOLDER_FALLBACK", default=False)

        self.fal_base_url = (_env_str("COMMERCE_FAL_BASE_URL", "https://queue.fal.run") or "https://queue.fal.run").rstrip("/")

        # Leffa endpoint (legacy default)
        self.fal_leffa_endpoint_id = (
            _env_str("COMMERCE_FAL_ENDPOINT_ID", "fal-ai/leffa/virtual-tryon") or "fal-ai/leffa/virtual-tryon"
        ).strip().strip("/")

        # FASHN endpoint (default v1.5; can override to v1.6)
        self.fal_fashn_endpoint_id = (
            _env_str("COMMERCE_FAL_FASHN_ENDPOINT_ID", "fal-ai/fashn/tryon/v1.5") or "fal-ai/fashn/tryon/v1.5"
        ).strip().strip("/")

        # Choose saree provider route
        self.saree_provider = (_env_str("COMMERCE_SAREE_PROVIDER", "fashn") or "fashn").strip().lower()
        if self.saree_provider not in ("fashn", "leffa"):
            self.saree_provider = "fashn"

        self.fal_status_endpoint_id_override = (_env_str("COMMERCE_FAL_STATUS_ENDPOINT_ID", "") or "").strip().strip("/")

        self.fal_poll_timeout_s = _clamp_int(_env_str("COMMERCE_FAL_POLL_TIMEOUT_S", "180"), default=180, lo=30, hi=900)
        self.fal_poll_secs = max(0.25, _coerce_float(_env_str("COMMERCE_FAL_POLL_SECS", "1.5") or "1.5", 1.5))
        self.fal_timeout_s = _clamp_int(_env_str("COMMERCE_FAL_HTTP_TIMEOUT_S", "120"), default=120, lo=20, hi=600)
        self.fal_poll_logs = _env_bool("COMMERCE_FAL_POLL_LOGS", default=False)

        # FASHN tuning
        self.fashn_mode = (_env_str("COMMERCE_FASHN_MODE", "quality") or "quality").strip().lower()
        if self.fashn_mode not in ("performance", "balanced", "quality"):
            self.fashn_mode = "quality"

        self.fashn_category_saree = (_env_str("COMMERCE_FASHN_CATEGORY_SAREE", "one-pieces") or "one-pieces").strip().lower()
        if self.fashn_category_saree not in ("tops", "bottoms", "one-pieces", "auto"):
            self.fashn_category_saree = "one-pieces"

        self.fashn_garment_photo_type = (_env_str("COMMERCE_FASHN_GARMENT_PHOTO_TYPE", "auto") or "auto").strip().lower()
        if self.fashn_garment_photo_type not in ("auto", "model", "flat-lay"):
            self.fashn_garment_photo_type = "auto"

        self.fashn_moderation_level = (_env_str("COMMERCE_FASHN_MODERATION_LEVEL", "permissive") or "permissive").strip().lower()
        if self.fashn_moderation_level not in ("none", "permissive", "conservative"):
            self.fashn_moderation_level = "permissive"

        self.fashn_segmentation_free = _env_bool("COMMERCE_FASHN_SEGMENTATION_FREE", default=True)

        self.fashn_output_format = (_env_str("COMMERCE_FASHN_OUTPUT_FORMAT", "png") or "png").strip().lower()
        if self.fashn_output_format not in ("png", "jpeg"):
            self.fashn_output_format = "png"

        self.fashn_sync_mode = _env_bool("COMMERCE_FASHN_SYNC_MODE", default=False)

    def _fal_key(self) -> str:
        return (_env_str("FAL_KEY", "") or _env_str("FAL_API_KEY", "") or _env_str("COMMERCE_FAL_KEY", "")).strip()

    def _fal_status_endpoint_id_for(self, endpoint_id: str) -> str:
        if self.fal_status_endpoint_id_override:
            return self.fal_status_endpoint_id_override
        parts = [p for p in (endpoint_id or "").split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[:2])
        return endpoint_id

    def _placeholder_url(self, *, product_type: str, pose: str, bg: str, idx: int) -> str:
        txt = f"vton+{product_type}+{pose}+{bg}+{idx}"
        return f"{self.placeholder_base}/1024x1024/png?text={txt}"

    def _stable_seed(self, *, request_hash: str, idx: int) -> int:
        h = _sha256(f"{request_hash}:{idx}")
        return int(h[:8], 16) & 0x7FFFFFFF

    def _resolve_human_image_url(self, *, model_ref: Dict[str, Any]) -> Optional[str]:
        for k in ("human_image_url", "image_url", "url", "ref_url", "photo_url"):
            v = model_ref.get(k)
            if isinstance(v, str) and v.strip().startswith("http"):
                return v.strip()
        if self.demo_mode:
            return _env_str(
                "COMMERCE_DEMO_HUMAN_IMAGE_URL",
                "https://storage.googleapis.com/falserverless/model_tests/leffa/person_image.jpg",
            )
        return None

    def _resolve_outfit_components(self, *, product_assets: Dict[str, Any]) -> Dict[str, Any]:
        """
        Returns:
          {
            "primary_garment_url": str|None,
            "saree_url": str|None,
            "blouse_url": str|None,
            "jewelry_urls": [{"component_code":..., "image_url":...}, ...],
            "items_norm": [ ... ]  # normalized items (best-effort)
          }
        """
        out: Dict[str, Any] = {
            "primary_garment_url": None,
            "saree_url": None,
            "blouse_url": None,
            "jewelry_urls": [],
            "items_norm": [],
        }

        # legacy top-level fields
        saree_url = product_assets.get("saree_image_url")
        blouse_url = product_assets.get("blouse_image_url")
        garment_url = (
            product_assets.get("garment_image_url")
            or product_assets.get("primary_image_url")
            or product_assets.get("product_image_url")
        )

        u = _first_http_str(saree_url)
        if u:
            out["saree_url"] = u
        u = _first_http_str(blouse_url)
        if u:
            out["blouse_url"] = u
        u = _first_http_str(garment_url)
        if u:
            out["primary_garment_url"] = u

        items = _as_list(product_assets.get("items"))
        dominant = str(product_assets.get("dominant_component_code") or "").strip().lower()

        for it in items:
            d = _as_dict(it)
            code = str(d.get("component_code") or d.get("kind") or d.get("type") or "").strip().lower()
            name = str(d.get("name") or "").strip().lower()

            # support url in multiple shapes
            img = (
                _first_http_str(d.get("image_url"))
                or _first_http_str(d.get("url"))
                or _first_http_str(d.get("image_urls"))
                or _first_http_str(d.get("urls"))
                or _first_http_str(d.get("images"))
            )

            norm = {"component_code": code, "name": name, "image_url": img, "kind": code, "category": d.get("category")}
            out["items_norm"].append(norm)

            if code == "saree" and img:
                out["saree_url"] = out["saree_url"] or img
            elif code in ("blouse", "choli") and img:
                out["blouse_url"] = out["blouse_url"] or img
            elif code.startswith("jewelry_") and img:
                out["jewelry_urls"].append({"component_code": code, "image_url": img})
            elif ("jewel" in code or "jewel" in name or "accessory" in (str(d.get("category") or "").lower())) and img:
                out["jewelry_urls"].append({"component_code": code or "jewelry", "image_url": img})

            if dominant and code == dominant and img:
                out["primary_garment_url"] = img

        if out["saree_url"] and not out["primary_garment_url"]:
            out["primary_garment_url"] = out["saree_url"]

        return out

    def _is_saree_like(self, *, product_assets: Dict[str, Any], garment_url: Optional[str]) -> bool:
        if product_assets.get("saree_image_url"):
            return True
        if str(product_assets.get("outfit_kind") or "").strip().lower() in ("saree", "saree_set", "saree+blouse", "sari", "saari"):
            return True

        items = _as_list(product_assets.get("items"))
        for it in items:
            d = _as_dict(it)
            code = str(d.get("component_code") or d.get("kind") or "").strip().lower()
            name = str(d.get("name") or "").strip().lower()
            if code == "saree" or "saree" in name or "sari" in name or "saari" in name:
                return True

        blob = " ".join(
            [
                str(product_assets.get("title") or ""),
                str(product_assets.get("name") or ""),
                str(product_assets.get("category") or ""),
                str(garment_url or ""),
            ]
        ).lower()
        tokens = ("saree", "sari", "saari", "pallu", "pleat", "kanjivaram", "banarasi")
        return any(t in blob for t in tokens)

    def _infer_garment_type(self, *, product_assets: Dict[str, Any], garment_url: Optional[str]) -> str:
        gt = str(product_assets.get("garment_type") or "").strip().lower()
        if gt in ("upper_body", "lower_body", "dresses"):
            return gt

        if product_assets.get("saree_image_url"):
            return "dresses"

        items = _as_list(product_assets.get("items"))
        for it in items:
            d = _as_dict(it)
            code = str(d.get("component_code") or d.get("kind") or "").strip().lower()
            name = str(d.get("name") or "").strip().lower()
            if code == "saree" or "saree" in name or "sari" in name or "saari" in name:
                return "dresses"

        u = (garment_url or "").lower()
        if any(t in u for t in ("saree", "sari", "saari", "pallu", "pleat", "kanjivaram", "banarasi")):
            return "dresses"

        ct = str(product_assets.get("cloth_type") or "").strip().lower()
        if ct in ("upper", "upper_body"):
            return "upper_body"
        if ct in ("lower", "lower_body"):
            return "lower_body"
        if ct in ("dress", "dresses", "full", "full_body", "overall"):
            return "dresses"

        if any(t in u for t in ("jeans", "pant", "pants", "trouser", "skirt", "shorts")):
            return "lower_body"
        if any(t in u for t in ("dress", "gown", "anarkali", "lehenga", "salwar", "dupatta")):
            return "dresses"

        return "upper_body"

    async def _fal_run_and_wait(
        self,
        *,
        fal_key: str,
        endpoint_id: str,
        input_json: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        headers = {"Authorization": f"Key {fal_key}"}

        post_url = f"{self.fal_base_url}/{endpoint_id}"
        submit = await asyncio.to_thread(
            _http_json, "POST", post_url, headers=headers, data=input_json, timeout_s=self.fal_timeout_s
        )

        request_id = str(submit.get("request_id") or "").strip()
        if not request_id:
            raise RuntimeError(f"Fal queue did not return request_id. submit={submit}")

        status_url = str(submit.get("status_url") or "").strip()
        result_url = str(submit.get("response_url") or "").strip()

        status_endpoint_id = self._fal_status_endpoint_id_for(endpoint_id)
        if not status_url.startswith("http"):
            status_url = f"{self.fal_base_url}/{status_endpoint_id}/requests/{request_id}/status"
        if not result_url.startswith("http"):
            result_url = f"{self.fal_base_url}/{status_endpoint_id}/requests/{request_id}"

        poll_status_url = status_url
        if self.fal_poll_logs and "?" not in poll_status_url:
            poll_status_url = f"{poll_status_url}?logs=1"
        elif self.fal_poll_logs and "logs=" not in poll_status_url:
            poll_status_url = f"{poll_status_url}&logs=1"

        t0 = time.time()
        last_status: Dict[str, Any] = submit

        while True:
            st = await asyncio.to_thread(
                _http_json, "GET", poll_status_url, headers=headers, data=None, timeout_s=self.fal_timeout_s
            )
            last_status = st
            s = str(st.get("status") or "").upper()

            if s == "COMPLETED":
                break

            if time.time() - t0 > float(self.fal_poll_timeout_s):
                raise RuntimeError(f"Fal queue timed out waiting COMPLETED. request_id={request_id} last_status={st}")

            await asyncio.sleep(self.fal_poll_secs)

        out = await asyncio.to_thread(_http_json, "GET", result_url, headers=headers, data=None, timeout_s=self.fal_timeout_s)
        dbg = {
            "request_id": request_id,
            "post_url": post_url,
            "endpoint_id": endpoint_id,
            "status_endpoint_id": status_endpoint_id,
            "status_url": status_url,
            "poll_status_url": poll_status_url,
            "result_url": result_url,
            "last_status": last_status,
        }
        return out, dbg

    def _parse_leffa_url(self, out_d: Dict[str, Any]) -> str:
        image = _as_dict(out_d.get("image"))
        url = image.get("url") if isinstance(image.get("url"), str) else ""
        if isinstance(url, str) and url.strip().startswith("http"):
            return url.strip()

        # best-effort alternate schema
        images = out_d.get("images")
        if isinstance(images, list) and images:
            u = _as_dict(images[0]).get("url")
            if isinstance(u, str) and u.strip().startswith("http"):
                return u.strip()

        return ""

    def _parse_fashn_urls(self, out_d: Dict[str, Any]) -> List[str]:
        images = out_d.get("images")
        urls: List[str] = []
        if isinstance(images, list):
            for it in images:
                d = _as_dict(it)
                u = d.get("url")
                if isinstance(u, str) and u.strip().startswith("http"):
                    urls.append(u.strip())
        return urls

    async def _try_saree_drape_pipeline(
        self,
        *,
        req: VTONGenerateRequest,
        human_url: str,
        product_assets: Dict[str, Any],
        model_ref: Dict[str, Any],
        default_drape_style: str,
        garment_type: str,
        comps: Dict[str, Any],
        n_real: int,
        urls_fallback: List[str],
    ) -> Optional[VTONGenerateResult]:
        """
        Attempts SareeDrapeProvider (internal). On any error returns None so caller can fallback to FASHN/Leffa.
        """
        if not self.enable_saree_drape_provider:
            return None

        try:
            # local imports to avoid cycles / heavy deps at boot
            from app.services.providers.saree_drape_provider import SareeDrapeProvider
            from app.services.drape.blender_runner import BlenderRunner
            from app.services.refine.saree_refiner import SareeRefiner

            # optional deps (best effort)
            storage = None
            fal_scene_client = None
            internal_client = None

            try:
                from app.services.azure_storage_service import AzureStorageService

                storage = AzureStorageService()
            except Exception as e:
                logger.warning("SareeDrapeProvider: AzureStorageService unavailable: %s", e)

            try:
                from app.services.providers.fal_scene_client import FalSceneClient

                fal_scene_client = FalSceneClient()
            except Exception as e:
                logger.warning("SareeDrapeProvider: FalSceneClient unavailable: %s", e)

            try:
                from app.services.providers.internal_pipeline_client import InternalPipelineClient

                internal_client = InternalPipelineClient()
            except Exception as e:
                logger.warning("SareeDrapeProvider: InternalPipelineClient unavailable: %s", e)

            saree_refiner = SareeRefiner(storage=storage, fal_scene_client=fal_scene_client, internal_client=internal_client)

            saree_provider = SareeDrapeProvider(
                storage=storage,
                blender_runner=BlenderRunner(),
                saree_refiner=saree_refiner,
            )

            # Build normalized items the saree provider can understand (kind/url)
            items_norm: List[Dict[str, Any]] = []
            for it in _as_list(product_assets.get("items")):
                d = _as_dict(it)
                code = str(d.get("component_code") or d.get("kind") or d.get("type") or "").strip().lower()
                name = str(d.get("name") or "").strip()
                img = (
                    _first_http_str(d.get("image_url"))
                    or _first_http_str(d.get("url"))
                    or _first_http_str(d.get("image_urls"))
                    or _first_http_str(d.get("urls"))
                    or _first_http_str(d.get("images"))
                )
                items_norm.append({"kind": code, "name": name, "category": d.get("category"), "url": img, "image_url": img})

            # Ensure saree/blouse are present as items if provided via top-level fields
            if comps.get("saree_url") and not any((i.get("kind") == "saree" and i.get("url")) for i in items_norm):
                items_norm.append({"kind": "saree", "name": "saree", "url": comps.get("saree_url"), "image_url": comps.get("saree_url")})
            if comps.get("blouse_url") and not any((i.get("kind") in ("blouse", "choli") and i.get("url")) for i in items_norm):
                items_norm.append({"kind": "blouse", "name": "blouse", "url": comps.get("blouse_url"), "image_url": comps.get("blouse_url")})
            for j in _as_list(comps.get("jewelry_urls")):
                jd = _as_dict(j)
                ju = _first_http_str(jd.get("image_url")) or _first_http_str(jd.get("url"))
                kc = str(jd.get("component_code") or "jewelry").strip().lower()
                if ju:
                    items_norm.append({"kind": kc if kc else "jewelry", "name": kc, "url": ju, "image_url": ju, "category": "jewelry"})

            # resolved_inputs / request envelope for SareeDrapeProvider
            base_views = _as_dict(model_ref.get("views"))
            full_body_flag = bool(model_ref.get("full_body") or base_views.get("full_body"))

            def build_env_for_style(style: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
                resolved_inputs = {
                    "outfit_kind": "saree_set",
                    "saree_like": True,
                    "drape_style": style,
                    "items": items_norm,
                    "product_assets": {"items": items_norm},
                    "views": {"full_body": full_body_flag, **base_views},
                    "model_ref": {"url": human_url, "full_body": full_body_flag, "shot": model_ref.get("shot")},
                    "garment_type": garment_type,
                }
                request_env = {
                    "input": {"product_assets": {"items": items_norm}, "views": {"full_body": full_body_flag, **base_views}, "model_ref": {"url": human_url}},
                    "product_assets": {"items": items_norm},
                    "model_ref": {"url": human_url, "full_body": full_body_flag},
                    "items": items_norm,
                }
                return request_env, resolved_inputs

            urls: List[str] = []
            debug: List[Dict[str, Any]] = []
            for i in range(n_real):
                v = req.variants[i]
                style = (v.drape_style or default_drape_style or "nivi").strip().lower()

                request_env, resolved_inputs = build_env_for_style(style)

                # Run in thread (provider uses requests / filesystem)
                out = await asyncio.to_thread(
                    saree_provider.run,
                    job_id=req.studio_job_id,
                    user_id=req.user_id,
                    request=request_env,
                    resolved_inputs=resolved_inputs,
                )

                out_d = _as_dict(out)
                out_url = str(out_d.get("output_url") or out_d.get("url") or "").strip()

                # Require a URL output; otherwise treat as failure and fallback
                if not out_url.startswith("http"):
                    raise RuntimeError(f"SareeDrapeProvider produced non-url output: {out_url}")

                if out_url == human_url or out_url == (comps.get("saree_url") or ""):
                    raise RuntimeError("SareeDrapeProvider produced no-op output (matched input url)")

                urls.append(out_url)
                debug.append({"i": i, "url": out_url, "drape_style": style, "provider_debug": out_d.get("debug")})

            if n_real < len(req.variants):
                urls.extend(urls_fallback[n_real:])

            return VTONGenerateResult(
                provider="saree_drape",
                urls=urls,
                meta={
                    "route": "saree_drape_provider",
                    "variant_count": len(req.variants),
                    "provider_images": n_real,
                    "saree_like": True,
                    "resolved_inputs": {
                        "saree_like": True,
                        "outfit_kind": "saree_set",
                        "drape_style": default_drape_style,
                        "garment_type": garment_type,
                        "human_image_url": human_url,
                        "saree_image_url": comps.get("saree_url"),
                        "blouse_image_url": comps.get("blouse_url"),
                        "garment_image_url": comps.get("primary_garment_url"),
                        "jewelry_urls": comps.get("jewelry_urls") or [],
                        "items_norm_count": len(_as_list(comps.get("items_norm"))),
                    },
                    "debug": debug[:3],
                },
            )

        except Exception as e:
            logger.warning("SareeDrapeProvider failed; will fall back to FASHN/Leffa. err=%s", e)
            return None

    async def generate(self, req: VTONGenerateRequest) -> VTONGenerateResult:
        product_assets = _as_dict(req.product_assets)
        model_ref = _as_dict(req.model_ref)

        product_type = str(product_assets.get("product_type") or "apparel").lower()

        urls_fallback = [
            self._placeholder_url(product_type=product_type, pose=v.pose, bg=v.background, idx=i)
            for i, v in enumerate(req.variants)
        ]

        if not self.enable_real:
            return VTONGenerateResult(
                provider="placeholder",
                urls=urls_fallback,
                meta={"note": "COMMERCE_ENABLE_REAL_PROVIDERS is off; using placeholders", "variant_count": len(urls_fallback)},
            )

        if self.provider != "fal":
            raise RuntimeError(f"VTONProvider: unsupported provider={self.provider!r} (only 'fal' is implemented here)")

        fal_key = self._fal_key()
        if not fal_key:
            raise RuntimeError("VTONProvider: missing FAL_KEY (or FAL_API_KEY / COMMERCE_FAL_KEY)")

        human_url = self._resolve_human_image_url(model_ref=model_ref)
        comps = self._resolve_outfit_components(product_assets=product_assets)

        garment_url = comps.get("primary_garment_url")
        saree_url = comps.get("saree_url")
        blouse_url = comps.get("blouse_url")
        jewelry_urls = comps.get("jewelry_urls") or []

        if not human_url or not garment_url:
            raise RuntimeError(
                "VTONProvider: missing human_image_url or garment_image_url. "
                "Provide model_ref.human_image_url and product_assets.garment_image_url (or items[] with dominant_component_code)."
            )

        saree_like = self._is_saree_like(product_assets=product_assets, garment_url=saree_url or garment_url)
        garment_type = self._infer_garment_type(product_assets=product_assets, garment_url=saree_url or garment_url)
        default_drape_style = str(product_assets.get("drape_style") or "").strip().lower() or "nivi"

        if self.enforce_full_body_for_saree and saree_like:
            shot = str(model_ref.get("shot") or model_ref.get("shot_type") or "").strip().lower()
            full_body_flag = model_ref.get("full_body")
            if full_body_flag is not True and shot not in ("full_body", "three_quarter"):
                raise RuntimeError(
                    "Saree try-on requires a full-body human image. "
                    "Set model_ref.full_body=true (or model_ref.shot='full_body') and provide a head-to-toe image."
                )

        n_real = min(len(req.variants), self.max_provider_images)

        urls: List[str] = []
        debug: List[Dict[str, Any]] = []

        try:
            # =========================================================
            # Saree-like: try SareeDrapeProvider first (if enabled)
            # =========================================================
            if saree_like and self.enable_saree_drape_provider:
                dr = await self._try_saree_drape_pipeline(
                    req=req,
                    human_url=human_url,
                    product_assets=product_assets,
                    model_ref=model_ref,
                    default_drape_style=default_drape_style,
                    garment_type=garment_type,
                    comps=comps,
                    n_real=n_real,
                    urls_fallback=urls_fallback,
                )
                if dr is not None:
                    return dr

            # =========================================================
            # Saree-like: FASHN (default) or Leffa (explicit)
            # =========================================================
            if saree_like and self.saree_provider == "fashn":
                endpoint_id = self.fal_fashn_endpoint_id

                remaining = n_real
                batch_start = 0
                while remaining > 0:
                    batch_n = min(4, remaining)
                    seed = self._stable_seed(request_hash=req.request_hash, idx=batch_start)

                    fashn_input: Dict[str, Any] = {
                        "model_image": human_url,
                        "garment_image": garment_url,
                        "category": self.fashn_category_saree,
                        "mode": self.fashn_mode,
                        "garment_photo_type": self.fashn_garment_photo_type,
                        "moderation_level": self.fashn_moderation_level,
                        "seed": int(seed),
                        "num_samples": int(batch_n),
                        "segmentation_free": bool(self.fashn_segmentation_free),
                        "output_format": self.fashn_output_format,
                    }
                    if self.fashn_sync_mode:
                        fashn_input["sync_mode"] = True

                    out, dbg = await self._fal_run_and_wait(
                        fal_key=fal_key,
                        endpoint_id=endpoint_id,
                        input_json=fashn_input,
                    )
                    out_d = _as_dict(out)
                    batch_urls = self._parse_fashn_urls(out_d)

                    if len(batch_urls) < batch_n:
                        raise RuntimeError(f"FASHN returned {len(batch_urls)} images, expected {batch_n}. out={out_d}")

                    # no-op sanity check
                    for u in batch_urls[:batch_n]:
                        if u == human_url or u == garment_url:
                            raise RuntimeError("FASHN returned a no-op URL matching an input")

                    for j, u in enumerate(batch_urls[:batch_n]):
                        idx = batch_start + j
                        eff_style = (req.variants[idx].drape_style or default_drape_style) if idx < len(req.variants) else default_drape_style
                        urls.append(u)
                        debug.append({"i": idx, "url": u, "seed": int(seed), "drape_style": eff_style, "dbg": dbg})

                    remaining -= batch_n
                    batch_start += batch_n

                if n_real < len(req.variants):
                    urls.extend(urls_fallback[n_real:])

                return VTONGenerateResult(
                    provider="fal",
                    urls=urls,
                    meta={
                        "route": "fashn_v1.x",
                        "endpoint_id": endpoint_id,
                        "status_endpoint_id": self._fal_status_endpoint_id_for(endpoint_id),
                        "variant_count": len(req.variants),
                        "provider_images": n_real,
                        "demo_mode": self.demo_mode,
                        "saree_like": True,
                        "resolved_inputs": {
                            "fashn": {
                                "mode": self.fashn_mode,
                                "category": self.fashn_category_saree,
                                "output_format": self.fashn_output_format,
                                "moderation_level": self.fashn_moderation_level,
                                "segmentation_free": self.fashn_segmentation_free,
                                "garment_photo_type": self.fashn_garment_photo_type,
                                "sync_mode": self.fashn_sync_mode,
                            },
                            "saree_like": True,
                            "drape_style": default_drape_style,
                            "garment_type": garment_type,
                            "jewelry_urls": jewelry_urls,
                            "human_image_url": human_url,
                            "saree_image_url": saree_url,
                            "blouse_image_url": blouse_url,
                            "garment_image_url": garment_url,
                            "effective_garment_type": "dresses",
                            "dominant_component_code": product_assets.get("dominant_component_code") or "primary_garment",
                            "items_norm_count": len(_as_list(comps.get("items_norm"))),
                        },
                        "variant_styles": [
                            {"i": ix, "drape_style": (req.variants[ix].drape_style or default_drape_style)}
                            for ix in range(min(len(req.variants), 12))
                        ],
                        "debug": [{"i": d["i"], "url": d["url"]} for d in debug[:5]],
                    },
                )

            # =========================================================
            # Default route: Leffa (also used for saree if explicitly chosen)
            # =========================================================
            endpoint_id = self.fal_leffa_endpoint_id

            sem = asyncio.Semaphore(min(4, n_real))

            async def run_one(i: int) -> Tuple[int, str, Dict[str, Any], int, str]:
                async with sem:
                    v = req.variants[i]
                    seed = v.seed if v.seed is not None else self._stable_seed(request_hash=req.request_hash, idx=i)

                    effective_garment_type = "dresses" if saree_like else garment_type
                    effective_drape_style = v.drape_style or default_drape_style

                    fal_input = {
                        "human_image_url": human_url,
                        "garment_image_url": garment_url,
                        "garment_type": effective_garment_type,
                        "seed": int(seed),
                        "guidance_scale": float(_env_str("COMMERCE_FAL_GUIDANCE_SCALE", "2.5") or "2.5"),
                        "num_inference_steps": _clamp_int(_env_str("COMMERCE_FAL_STEPS", "50"), default=50, lo=1, hi=50),
                        "output_format": str(_env_str("COMMERCE_FAL_OUTPUT_FORMAT", "png") or "png"),
                        "enable_safety_checker": _env_bool("COMMERCE_FAL_SAFETY", default=True),
                    }

                    out, dbg = await self._fal_run_and_wait(
                        fal_key=fal_key,
                        endpoint_id=endpoint_id,
                        input_json=fal_input,
                    )
                    out_d = _as_dict(out)

                    url = self._parse_leffa_url(out_d)
                    if not url.startswith("http"):
                        raise RuntimeError(f"Leffa returned invalid image url for variant={i}: out={out_d}")

                    if url == human_url or url == garment_url:
                        raise RuntimeError(f"Leffa returned input URL (no-op) for variant={i}: url={url}")

                    return i, url, dbg, int(seed), effective_drape_style

            results = await asyncio.gather(*[run_one(i) for i in range(n_real)])

            results_sorted = sorted(results, key=lambda t: t[0])
            for i, url, dbg, seed, eff_style in results_sorted:
                urls.append(url)
                debug.append({"i": i, "url": url, "seed": seed, "drape_style": eff_style, "dbg": dbg})

            if n_real < len(req.variants):
                urls.extend(urls_fallback[n_real:])

            return VTONGenerateResult(
                provider="fal",
                urls=urls,
                meta={
                    "route": "leffa",
                    "endpoint_id": endpoint_id,
                    "status_endpoint_id": self._fal_status_endpoint_id_for(endpoint_id),
                    "variant_count": len(req.variants),
                    "provider_images": n_real,
                    "demo_mode": self.demo_mode,
                    "saree_like": saree_like,
                    "resolved_inputs": {
                        "human_image_url": human_url,
                        "garment_image_url": garment_url,
                        "saree_image_url": saree_url,
                        "blouse_image_url": blouse_url,
                        "jewelry_urls": jewelry_urls,
                        "garment_type": garment_type,
                        "effective_garment_type": "dresses" if saree_like else garment_type,
                        "dominant_component_code": product_assets.get("dominant_component_code") or "primary_garment",
                        "saree_like": saree_like,
                        "drape_style": default_drape_style,
                        "items_norm_count": len(_as_list(comps.get("items_norm"))),
                    },
                    "variant_styles": [
                        {"i": ix, "drape_style": (req.variants[ix].drape_style or default_drape_style)}
                        for ix in range(min(len(req.variants), 12))
                    ],
                    "debug": [{"i": d["i"], "url": d["url"]} for d in debug[:5]],
                },
            )

        except Exception as e:
            logger.exception("VTONProvider.generate failed: %s", e)
            if self.allow_placeholder_fallback:
                return VTONGenerateResult(
                    provider="fal_failed_fallback",
                    urls=urls_fallback,
                    meta={
                        "error": f"{type(e).__name__}: {e}",
                        "note": "real provider failed; using placeholders",
                        "variant_count": len(urls_fallback),
                    },
                )
            raise