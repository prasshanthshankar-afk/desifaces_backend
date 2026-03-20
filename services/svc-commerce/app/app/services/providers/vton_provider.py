from __future__ import annotations

import re
import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from app.services.providers.vton.indian_non_saree_router import (
    build_required_pipeline_error,
    is_phase1_indian_non_saree_family,
    resolve_indian_non_saree_routing,
)

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Tiny helpers
# -----------------------------------------------------------------------------


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


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        v = (os.getenv(name) or "").strip()
        return float(v) if v else default
    except Exception:
        return default


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


def _truthy(v: Any) -> bool:
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "y", "on"):
        return True
    return False


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


def _norm_text(x: Any) -> str:
    return str(x or "").strip().lower()


def _uniq_norm(xs: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in _as_list(xs):
        s = _norm_text(x)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _text_blob(*parts: Any) -> str:
    vals: List[str] = []
    for p in parts:
        s = _norm_text(p)
        if s:
            vals.append(s)
    return " ".join(vals)


_SUPPORTED_NON_SAREE_PROVIDERS = {"imageapps_v2", "fashn", "catvton"}


def _csv_tokens(v: Any) -> List[str]:
    out: List[str] = []
    for part in str(v or "").split(","):
        s = _norm_text(part)
        if s:
            out.append(s)
    return out


_IMAGEAPPS_ALLOWED_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4"}


def _imageapps_aspect_ratio_obj(v: Any, *, default_ratio: str = "3:4") -> Dict[str, str]:
    """
    fal-ai/image-apps-v2/virtual-try-on expects:
      aspect_ratio: {"ratio": "3:4"}  (NOT "3:4")
    """
    if isinstance(v, dict):
        r = str(v.get("ratio") or "").strip()
        if r in _IMAGEAPPS_ALLOWED_RATIOS:
            return {"ratio": r}

    if isinstance(v, str):
        s = v.strip()
        if s in _IMAGEAPPS_ALLOWED_RATIOS:
            return {"ratio": s}
        m = re.match(r"^\s*(\d+)\s*[:x/]\s*(\d+)\s*$", s)
        if m:
            cand = f"{m.group(1)}:{m.group(2)}"
            if cand in _IMAGEAPPS_ALLOWED_RATIOS:
                return {"ratio": cand}

    if default_ratio not in _IMAGEAPPS_ALLOWED_RATIOS:
        default_ratio = "3:4"
    return {"ratio": default_ratio}


def _http_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    data: Optional[Dict[str, Any]] = None,
    timeout_s: int = 120,
) -> Dict[str, Any]:
    """
    Safe JSON HTTP helper.

    IMPORTANT:
      - For GET/HEAD: DO NOT send a body.
      - Retry transient 5xx ONLY for GET/HEAD (safe).
    """
    m = (method or "GET").strip().upper()

    hdrs: Dict[str, str] = {"Accept": "application/json", "User-Agent": "df-svc-commerce/1.0"}
    hdrs.update(headers or {})

    body: Optional[bytes] = None
    if m not in ("GET", "HEAD") and data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    retries = 3 if m in ("GET", "HEAD") else 0

    for attempt in range(retries + 1):
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
            if m in ("GET", "HEAD") and e.code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(0.8 * (attempt + 1))
                continue

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
            raise RuntimeError(f"HTTPError code={e.code} url={url} body={j}") from e

        except URLError as e:
            raise RuntimeError(f"URLError url={url} err={e}") from e

    return {}


def _fashn_category_for_garment_type(gt: str) -> str:
    gt = (gt or "").strip().lower()
    if gt == "upper_body":
        return "tops"
    if gt == "lower_body":
        return "bottoms"
    if gt == "dresses":
        return "one-pieces"
    return "auto"


def _construct_with_supported_kwargs(cls: Any, **candidate_kwargs: Any) -> Any:
    """
    Instantiate cls(**kwargs) but only pass keyword args that cls.__init__ accepts.
    """
    sig = inspect.signature(cls.__init__)
    params = sig.parameters
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if accepts_var_kw:
        return cls(**candidate_kwargs)

    allowed = {k for k in params.keys() if k != "self"}
    filtered = {k: v for k, v in candidate_kwargs.items() if k in allowed}
    return cls(**filtered)


def _variant_job_id(*, job_id: UUID, variant_index: int) -> str:
    return f"{str(job_id)}-{int(variant_index)}"


def _download_bytes(url: str, *, timeout_s: int = 180) -> bytes:
    req = Request(url, headers={"User-Agent": "df-vton-uploader"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read() or b""


def _guess_ext_and_content_type(url: str, *, default_ext: str = ".png") -> Tuple[str, str]:
    u = (url or "").split("?", 1)[0].lower()
    if u.endswith(".jpg") or u.endswith(".jpeg"):
        return ".jpg", "image/jpeg"
    if u.endswith(".webp"):
        return ".webp", "image/webp"
    if u.endswith(".png"):
        return ".png", "image/png"
    return default_ext, ("image/png" if default_ext == ".png" else "application/octet-stream")


def _is_http_url(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith(("http://", "https://"))


def _parse_az_ref(s: str) -> Optional[Tuple[str, str]]:
    v = (s or "").strip()
    if not v.startswith("az://"):
        return None
    rest = v[len("az://") :]
    if "/" not in rest:
        return None
    c, b = rest.split("/", 1)
    c = c.strip()
    b = b.lstrip("/")
    if not c or not b:
        return None
    return c, b


def _get_storage_service_best_effort():
    """
    Best-effort init for AzureStorageService without assuming a single constructor signature.
    """
    try:
        from app.services.azure_storage_service import AzureStorageConfig, AzureStorageService
    except Exception as e:
        raise RuntimeError(f"missing_azure_storage_service: {e}") from e

    try:
        return AzureStorageService()
    except Exception as e:
        conn = (
            (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
            or (os.getenv("COMMERCE_AZURE_STORAGE_CONNECTION_STRING") or "").strip()
            or (os.getenv("DF_AZURE_STORAGE_CONNECTION_STRING") or "").strip()
        )
        if not conn:
            raise RuntimeError(f"missing_azure_connection_string err={e!r}") from e

        fallback_container = (os.getenv("COMMERCE_OUTPUT_CONTAINER") or "commerce-output").strip() or "commerce-output"
        cfg = AzureStorageConfig(connection_string=conn, container=fallback_container, default_sas_hours=24)
        return AzureStorageService(config=cfg)


def _call_any_upload_method(storage: Any, *, container: str, blob_name: str, data: bytes, content_type: str) -> None:
    """
    Try common AzureStorageService upload method names + signatures.
    """
    candidates = [
        "upload_bytes",
        "upload_blob",
        "put_blob",
        "upload_content",
        "upload",
    ]

    last_err: Optional[Exception] = None

    for name in candidates:
        fn = getattr(storage, name, None)
        if not fn or not callable(fn):
            continue

        try:
            sig = inspect.signature(fn)
            allowed = set(sig.parameters.keys())
        except Exception:
            allowed = set()

        kw: Dict[str, Any] = {}
        if "container" in allowed:
            kw["container"] = container
        if "blob_name" in allowed:
            kw["blob_name"] = blob_name
        if "name" in allowed:
            kw["name"] = blob_name
        if "path" in allowed:
            kw["path"] = blob_name
        if "blob" in allowed:
            kw["blob"] = blob_name
        if "data" in allowed:
            kw["data"] = data
        if "content" in allowed:
            kw["content"] = data
        if "bytes" in allowed:
            kw["bytes"] = data
        if "content_type" in allowed:
            kw["content_type"] = content_type
        if "mime_type" in allowed:
            kw["mime_type"] = content_type

        try:
            if kw:
                fn(**kw)
            else:
                fn(blob_name, data, content_type)
            return
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"azure_upload_failed: no compatible upload method found err={last_err!r}")


def _call_any_sas_method(storage: Any, *, container: str, blob_name: str, expires_in_s: int, permission: str) -> str:
    fn = getattr(storage, "get_blob_sas_url", None)
    if not fn or not callable(fn):
        raise RuntimeError("azure_missing_get_blob_sas_url")

    try:
        sig = inspect.signature(fn)
        allowed = set(sig.parameters.keys())
    except Exception:
        allowed = set()

    kw: Dict[str, Any] = {}
    if "container" in allowed:
        kw["container"] = container
    if "blob_name" in allowed:
        kw["blob_name"] = blob_name
    if "expires_in_s" in allowed:
        kw["expires_in_s"] = int(expires_in_s)
    if "permission" in allowed:
        kw["permission"] = permission

    if kw:
        return str(fn(**kw))
    return str(fn(container, blob_name, expires_in_s, permission))


# -----------------------------------------------------------------------------
# Domain
# -----------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# Fal queue client (shared by providers)
# -----------------------------------------------------------------------------


class FalQueueClient:
    def __init__(self) -> None:
        self.base_url = (_env_str("COMMERCE_FAL_BASE_URL", "https://queue.fal.run") or "https://queue.fal.run").rstrip("/")
        self.http_timeout_s = _clamp_int(_env_str("COMMERCE_FAL_HTTP_TIMEOUT_S", "180"), default=180, lo=20, hi=600)
        self.poll_timeout_s = _clamp_int(_env_str("COMMERCE_FAL_POLL_TIMEOUT_S", "900"), default=900, lo=30, hi=1800)
        self.poll_secs = max(0.25, _coerce_float(_env_str("COMMERCE_FAL_POLL_SECS", "1.5") or "1.5", 1.5))
        self.poll_logs = _env_bool("COMMERCE_FAL_POLL_LOGS", default=False)

    def _fal_key(self) -> str:
        return (_env_str("FAL_KEY", "") or _env_str("FAL_API_KEY", "") or _env_str("COMMERCE_FAL_KEY", "")).strip()

    def _status_endpoint_id_for(self, endpoint_id: str) -> str:
        parts = [p for p in (endpoint_id or "").split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[:2])
        return endpoint_id

    async def run_and_wait(self, *, endpoint_id: str, input_json: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        fal_key = self._fal_key()
        if not fal_key:
            raise RuntimeError("missing_fal_key")

        headers = {"Authorization": f"Key {fal_key}"}
        post_url = f"{self.base_url}/{endpoint_id.strip().strip('/')}"

        submit = await asyncio.to_thread(_http_json, "POST", post_url, headers=headers, data=input_json, timeout_s=self.http_timeout_s)
        request_id = str(submit.get("request_id") or "").strip()
        if not request_id:
            raise RuntimeError(f"Fal queue did not return request_id. endpoint_id={endpoint_id} submit={submit}")

        status_url = str(submit.get("status_url") or "").strip()
        result_url = str(submit.get("response_url") or "").strip()

        status_ep = self._status_endpoint_id_for(endpoint_id)

        if not status_url.startswith("http"):
            status_url = f"{self.base_url}/{status_ep}/requests/{request_id}/status"
        if not result_url.startswith("http"):
            result_url = f"{self.base_url}/{status_ep}/requests/{request_id}"

        poll_status_url = status_url
        if self.poll_logs and "logs=" not in poll_status_url:
            poll_status_url = f"{poll_status_url}{'&' if '?' in poll_status_url else '?'}logs=1"

        t0 = time.time()
        last_status: Dict[str, Any] = submit if isinstance(submit, dict) else {}
        rewritten_405 = False

        while True:
            try:
                st = await asyncio.to_thread(_http_json, "GET", poll_status_url, headers=headers, data=None, timeout_s=self.http_timeout_s)
            except Exception as e:
                msg = repr(e)
                if ("HTTPError code=405" in msg or "code=405" in msg) and not rewritten_405:
                    rewritten_405 = True
                    poll_status_url = f"{self.base_url}/{status_ep}/requests/{request_id}/status"
                    result_url = f"{self.base_url}/{status_ep}/requests/{request_id}"
                    if self.poll_logs and "logs=" not in poll_status_url:
                        poll_status_url = f"{poll_status_url}{'&' if '?' in poll_status_url else '?'}logs=1"
                    await asyncio.sleep(self.poll_secs)
                    continue
                raise

            last_status = st
            s = str(st.get("status") or "").upper()
            if s == "COMPLETED":
                break
            if s in ("FAILED", "ERROR", "CANCELED", "CANCELLED"):
                raise RuntimeError(
                    f"Fal queue failed. endpoint_id={endpoint_id} request_id={request_id} status={s} last_status={st}"
                )
            if time.time() - t0 > float(self.poll_timeout_s):
                raise RuntimeError(
                    f"Fal queue timed out. endpoint_id={endpoint_id} request_id={request_id} last_status={st}"
                )

            await asyncio.sleep(self.poll_secs)

        out = await asyncio.to_thread(_http_json, "GET", result_url, headers=headers, data=None, timeout_s=self.http_timeout_s)
        dbg = {
            "request_id": request_id,
            "endpoint_id": endpoint_id,
            "status_endpoint_id": status_ep,
            "post_url": post_url,
            "poll_status_url": poll_status_url,
            "result_url": result_url,
            "last_status": last_status,
            "rewrote_405": bool(rewritten_405),
        }
        return out, dbg


def _parse_fal_any_image_urls(out_d: Dict[str, Any]) -> List[str]:
    urls: List[str] = []

    def _add(u: Any) -> None:
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)

    def _from_images_list(d: Dict[str, Any]) -> None:
        imgs = d.get("images")
        if isinstance(imgs, list):
            for it in imgs:
                _add(_as_dict(it).get("url"))

    def _from_image_obj(d: Dict[str, Any]) -> None:
        img = _as_dict(d.get("image"))
        _add(img.get("url"))

    roots = [out_d]
    for k in ("response", "output", "data"):
        if isinstance(out_d.get(k), dict):
            roots.append(_as_dict(out_d.get(k)))

    for r in roots:
        _from_images_list(r)
        _from_image_obj(r)

    if urls:
        out: List[str] = []
        seen = set()
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def scan(x: Any) -> None:
        if isinstance(x, dict):
            for kk, vv in x.items():
                if kk == "url" and isinstance(vv, str) and vv.startswith("http"):
                    urls.append(vv)
                else:
                    scan(vv)
        elif isinstance(x, list):
            for v in x:
                scan(v)

    scan(out_d)

    out2: List[str] = []
    seen2 = set()
    for u in urls:
        if u not in seen2:
            seen2.add(u)
            out2.append(u)
    return out2


# -----------------------------------------------------------------------------
# Saree routing helpers (UNCHANGED)
# -----------------------------------------------------------------------------


def _stable_seed(request_hash: str, idx: int) -> int:
    h = _sha256(f"{request_hash}:{idx}")
    return int(h[:8], 16) & 0x7FFFFFFF


def _resolve_human_image_url(*, model_ref: Dict[str, Any]) -> Optional[str]:
    for k in ("human_image_url", "image_url", "url", "ref_url", "photo_url"):
        v = model_ref.get(k)
        if isinstance(v, str) and v.strip().startswith("http"):
            return v.strip()

    meta = _as_dict(model_ref.get("meta"))
    for k in ("human_image_url", "image_url", "url", "ref_url", "photo_url"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip().startswith("http"):
            return v.strip()

    return None


def _resolve_outfit_components(*, product_assets: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "primary_garment_url": None,
        "saree_url": None,
        "blouse_url": None,
        "jewelry_urls": [],
        "items_norm": [],
    }

    saree_url = product_assets.get("saree_image_url")
    blouse_url = product_assets.get("blouse_image_url")
    garment_url = product_assets.get("garment_image_url") or product_assets.get("primary_image_url") or product_assets.get("product_image_url")

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

        img = (
            _first_http_str(d.get("image_url"))
            or _first_http_str(d.get("url"))
            or _first_http_str(d.get("asset_url"))
            or _first_http_str(_as_dict(d.get("asset")).get("url"))
        )

        out["items_norm"].append({"component_code": code, "name": name, "image_url": img, "kind": code, "category": d.get("category")})

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


def _is_saree_like(*, product_assets: Dict[str, Any], garment_url: Optional[str]) -> bool:
    if product_assets.get("saree_image_url"):
        return True
    if str(product_assets.get("outfit_kind") or "").strip().lower() in ("saree", "saree_set", "saree+blouse", "sari", "saari"):
        return True
    for it in _as_list(product_assets.get("items")):
        d = _as_dict(it)
        code = str(d.get("component_code") or d.get("kind") or "").strip().lower()
        name = str(d.get("name") or "").strip().lower()
        if code == "saree" or "saree" in name or "sari" in name or "saari" in name:
            return True
    blob = " ".join([str(product_assets.get("title") or ""), str(product_assets.get("name") or ""), str(product_assets.get("category") or ""), str(garment_url or "")]).lower()
    tokens = ("saree", "sari", "saari", "pallu", "pleat", "kanjivaram", "banarasi")
    return any(t in blob for t in tokens)


def _infer_garment_type(*, product_assets: Dict[str, Any], garment_url: Optional[str]) -> str:
    gt = str(product_assets.get("garment_type") or "").strip().lower()
    if gt in ("upper_body", "lower_body", "dresses"):
        return gt
    if product_assets.get("saree_image_url"):
        return "dresses"
    u = (garment_url or "").lower()

    if any(t in u for t in ("saree", "sari", "saari", "pallu", "pleat", "kanjivaram", "banarasi")):
        return "dresses"
    if any(t in u for t in ("jeans", "pant", "pants", "trouser", "skirt", "shorts", "lehenga", "dhoti")):
        return "lower_body"
    if any(t in u for t in ("dress", "gown", "jumpsuit", "anarkali", "salwar", "lehenga", "kurta", "suit")):
        return "dresses"
    if any(t in u for t in ("blazer", "hoodie", "jacket", "coat", "overcoat", "sweater", "cardigan")):
        return "upper_body"
    return "upper_body"


def _compute_full_body_flag(*, req: VTONGenerateRequest, model_ref: Dict[str, Any]) -> bool:
    base_views = _as_dict(model_ref.get("views"))
    meta = _as_dict(model_ref.get("meta"))
    meta_views = _as_dict(meta.get("views"))
    shot = str(model_ref.get("shot") or model_ref.get("shot_type") or meta.get("shot") or meta.get("shot_type") or "").strip().lower()

    full_body_flag = (
        _truthy(model_ref.get("full_body"))
        or _truthy(model_ref.get("is_full_body"))
        or _truthy(meta.get("full_body"))
        or _truthy(meta.get("is_full_body"))
        or _truthy(base_views.get("full_body"))
        or _truthy(meta_views.get("full_body"))
        or (shot in ("full_body", "three_quarter"))
    )

    req_views = _as_dict(_as_dict(req.model_ref).get("views"))
    req_meta_views = _as_dict(_as_dict(_as_dict(req.model_ref).get("meta")).get("views"))
    if _truthy(req_views.get("full_body")) or _truthy(req_meta_views.get("full_body")):
        full_body_flag = True

    return bool(full_body_flag)


# -----------------------------------------------------------------------------
# Saree QC (UNCHANGED)
# -----------------------------------------------------------------------------


class SareeQC:
    def __init__(self) -> None:
        self.enabled = _env_bool("COMMERCE_SAREE_QC_ENABLE", default=True)
        self.timeout_s = _clamp_int(_env_str("COMMERCE_SAREE_QC_TIMEOUT_S", "25"), default=25, lo=5, hi=120)
        self.image_size = _clamp_int(_env_str("COMMERCE_SAREE_QC_IMAGE_SIZE", "256"), default=256, lo=96, hi=512)

        self.min_lower_diff = _env_float("COMMERCE_SAREE_QC_MIN_LOWER_DIFF", 0.05)
        self.min_pallu_diff = _env_float("COMMERCE_SAREE_QC_MIN_PALLU_DIFF", 0.02)
        self.max_corner_diff = _env_float("COMMERCE_SAREE_QC_MAX_CORNER_DIFF", 0.10)
        self.max_face_diff = _env_float("COMMERCE_SAREE_QC_MAX_FACE_DIFF", 0.10)

    async def quick_gate(self, *, human_url: str, out_url: str) -> Tuple[bool, Dict[str, Any]]:
        if not self.enabled:
            return True, {"qc_enabled": False}

        from PIL import Image, ImageChops, ImageStat
        import io

        size = int(self.image_size)
        timeout_s = int(self.timeout_s)

        def _fetch(url: str) -> Image.Image:
            req = Request(url, headers={"User-Agent": "df-saree-qc"})
            raw = urlopen(req, timeout=timeout_s).read()
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            return im.resize((size, size))

        def _roi(im: Image.Image, x0: float, x1: float, y0: float, y1: float) -> Image.Image:
            W, H = im.size
            box = (int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))
            return im.crop(box)

        def _roi_diff(a: Image.Image, b: Image.Image, x0: float, x1: float, y0: float, y1: float) -> float:
            da = _roi(a, x0, x1, y0, y1)
            db = _roi(b, x0, x1, y0, y1)
            d = ImageChops.difference(da, db)
            st = ImageStat.Stat(d)
            mean = float(sum(st.mean) / max(1.0, float(len(st.mean))))
            return mean / 255.0

        human = await asyncio.to_thread(_fetch, human_url)
        out = await asyncio.to_thread(_fetch, out_url)

        lower = _roi_diff(human, out, 0.05, 0.95, 0.58, 0.98)
        p_left = _roi_diff(human, out, 0.05, 0.55, 0.18, 0.55)
        p_right = _roi_diff(human, out, 0.45, 0.95, 0.18, 0.55)
        pallu = max(p_left, p_right)

        c1 = _roi_diff(human, out, 0.00, 0.18, 0.00, 0.18)
        c2 = _roi_diff(human, out, 0.82, 1.00, 0.00, 0.18)
        c3 = _roi_diff(human, out, 0.00, 0.18, 0.82, 1.00)
        c4 = _roi_diff(human, out, 0.82, 1.00, 0.82, 1.00)
        corners = (c1 + c2 + c3 + c4) / 4.0

        face = _roi_diff(human, out, 0.30, 0.70, 0.00, 0.28)

        ok = (
            lower >= float(self.min_lower_diff)
            and pallu >= float(self.min_pallu_diff)
            and corners <= float(self.max_corner_diff)
            and face <= float(self.max_face_diff)
        )
        return bool(ok), {
            "qc_enabled": True,
            "ok": bool(ok),
            "lower": lower,
            "pallu": pallu,
            "corners": corners,
            "face": face,
            "thresholds": {
                "min_lower_diff": float(self.min_lower_diff),
                "min_pallu_diff": float(self.min_pallu_diff),
                "max_corner_diff": float(self.max_corner_diff),
                "max_face_diff": float(self.max_face_diff),
            },
        }


# -----------------------------------------------------------------------------
# Non-saree garment resolution / priors
# -----------------------------------------------------------------------------


def _infer_component_role(code: str, name: str, category: str) -> str:
    blob = " ".join([_norm_text(code), _norm_text(name), _norm_text(category)])

    if any(t in blob for t in ("jewelry", "necklace", "earring", "bangle", "ring", "accessory", "watch", "sunglass")):
        return "accessory"

    if any(t in blob for t in ("blazer", "jacket", "coat", "overcoat", "hoodie", "sweater", "cardigan", "outerwear")):
        return "outer"

    if any(t in blob for t in ("pants", "pant", "trouser", "jeans", "skirt", "shorts", "lehenga", "dhoti", "pyjama", "pajama")):
        return "lower"

    if any(t in blob for t in ("dress", "gown", "jumpsuit", "overall", "one-piece", "onepiece", "anarkali", "salwar", "suit", "kurta set", "lehenga set")):
        return "overall"

    if any(t in blob for t in ("shirt", "tshirt", "t-shirt", "top", "kurta", "kameez", "blouse", "choli", "sweatshirt")):
        return "upper"

    return "other"


def _looks_outerwear_url(u: Any) -> bool:
    s = str(u or "").lower()
    return any(t in s for t in ("blazer", "jacket", "coat", "overcoat", "hoodie", "cardigan", "outerwear", "trench"))


def _resolve_non_saree_garments(*, product_assets: Dict[str, Any], comps: Dict[str, Any]) -> Dict[str, Any]:
    items_norm = _as_list(comps.get("items_norm"))
    dominant = _norm_text(product_assets.get("dominant_component_code"))

    upper_url: Optional[str] = None
    lower_url: Optional[str] = None
    overall_url: Optional[str] = None
    outer_url: Optional[str] = None

    primary = _first_http_str(comps.get("primary_garment_url"))

    for it in items_norm:
        d = _as_dict(it)
        code = _norm_text(d.get("component_code") or d.get("kind"))
        name = _norm_text(d.get("name"))
        cat = _norm_text(d.get("category"))
        img = _first_http_str(d.get("image_url"))
        if not img:
            continue

        role = _infer_component_role(code, name, cat)

        if dominant and code == dominant:
            primary = primary or img

        if role == "overall":
            overall_url = overall_url or img
        elif role == "outer":
            outer_url = outer_url or img
        elif role == "upper":
            upper_url = upper_url or img
        elif role == "lower":
            lower_url = lower_url or img

    primary = primary or _first_http_str(product_assets.get("garment_image_url"))

    if overall_url:
        primary = overall_url

    if outer_url and (not primary or _looks_outerwear_url(primary) or _looks_outerwear_url(outer_url)):
        if not dominant or dominant in ("outer", "blazer", "jacket", "coat"):
            primary = outer_url

    primary = primary or upper_url or lower_url or outer_url

    return {
        "primary_url": primary,
        "upper_url": upper_url,
        "lower_url": lower_url,
        "overall_url": overall_url,
        "outer_url": outer_url,
        "items_norm": items_norm,
        "dominant_component_code": dominant,
    }


def _non_saree_platform_mode_requested(*, product_assets: Dict[str, Any], model_ref: Dict[str, Any]) -> bool:
    pa_meta = _as_dict(product_assets.get("meta"))
    mr_meta = _as_dict(model_ref.get("meta"))

    mode_blob = " ".join(
        [
            _norm_text(product_assets.get("mode")),
            _norm_text(pa_meta.get("mode")),
            _norm_text(model_ref.get("mode")),
            _norm_text(mr_meta.get("mode")),
            _norm_text(model_ref.get("source")),
            _norm_text(mr_meta.get("source")),
        ]
    )

    if "platform_models" in mode_blob:
        return True

    if _truthy(model_ref.get("use_platform_models")) or _truthy(mr_meta.get("use_platform_models")):
        return True

    if _truthy(model_ref.get("platform_model_required")) or _truthy(mr_meta.get("platform_model_required")):
        return True

    return False


def _resolve_platform_preferred_tags(*, product_assets: Dict[str, Any], model_ref: Dict[str, Any]) -> List[str]:
    pa_meta = _as_dict(product_assets.get("meta"))
    mr_meta = _as_dict(model_ref.get("meta"))
    tags: List[str] = []
    for src in (
        product_assets.get("style_tags"),
        pa_meta.get("style_tags"),
        model_ref.get("style_tags"),
        mr_meta.get("style_tags"),
        product_assets.get("preferred_tags"),
        pa_meta.get("preferred_tags"),
        model_ref.get("preferred_tags"),
        mr_meta.get("preferred_tags"),
    ):
        for t in _as_list(src):
            tt = _norm_text(t)
            if tt:
                tags.append(tt)
    return _uniq_norm(tags)


def _resolve_recent_platform_model_codes(*, product_assets: Dict[str, Any], model_ref: Dict[str, Any]) -> List[str]:
    pa_meta = _as_dict(product_assets.get("meta"))
    mr_meta = _as_dict(model_ref.get("meta"))
    codes: List[str] = []
    for src in (
        product_assets.get("recent_model_codes"),
        pa_meta.get("recent_model_codes"),
        model_ref.get("recent_model_codes"),
        mr_meta.get("recent_model_codes"),
    ):
        for c in _as_list(src):
            cc = _norm_text(c)
            if cc:
                codes.append(cc)
    return _uniq_norm(codes)


def _infer_non_saree_style_priors(
    *,
    product_assets: Dict[str, Any],
    ns: Dict[str, Any],
    garment_type: str,
    is_outerwear: bool,
) -> Dict[str, Any]:
    items_norm = _as_list(ns.get("items_norm"))

    blob_parts: List[str] = [
        _norm_text(product_assets.get("title")),
        _norm_text(product_assets.get("name")),
        _norm_text(product_assets.get("category")),
        _norm_text(product_assets.get("subcategory")),
        _norm_text(product_assets.get("garment_type")),
        _norm_text(product_assets.get("outfit_kind")),
        _norm_text(product_assets.get("dominant_component_code")),
        _norm_text(ns.get("primary_url")),
        _norm_text(ns.get("upper_url")),
        _norm_text(ns.get("lower_url")),
        _norm_text(ns.get("overall_url")),
        _norm_text(ns.get("outer_url")),
    ]

    for it in items_norm:
        d = _as_dict(it)
        blob_parts.extend(
            [
                _norm_text(d.get("component_code")),
                _norm_text(d.get("kind")),
                _norm_text(d.get("name")),
                _norm_text(d.get("category")),
                _norm_text(d.get("image_url")),
            ]
        )

    blob = " ".join([b for b in blob_parts if b])

    sleeveless_tokens = (
        "sleeveless",
        "strapless",
        "tube",
        "cami",
        "camisole",
        "tank",
        "halter",
        "spaghetti",
        "off shoulder",
        "off-shoulder",
    )
    sleeve_tokens = (
        "sleeve",
        "shirt",
        "kurta",
        "kameez",
        "blouse",
        "top",
        "tunic",
        "kaftan",
        "blazer",
        "jacket",
        "coat",
        "hoodie",
        "cardigan",
        "sweater",
        "gown",
        "dress",
    )
    long_body_tokens = (
        "kurta",
        "kameez",
        "tunic",
        "kaftan",
        "anarkali",
        "gown",
        "dress",
        "maxi",
        "longline",
        "long length",
        "long-length",
        "knee length",
        "knee-length",
        "calf length",
        "calf-length",
    )
    modest_side_tokens = (
        "kurta",
        "kameez",
        "kaftan",
        "anarkali",
        "blazer",
        "jacket",
        "coat",
        "hoodie",
        "cardigan",
        "sweater",
        "gown",
        "dress",
        "modest",
    )

    overall_like = bool(ns.get("overall_url")) or (garment_type == "dresses")
    explicitly_sleeveless = any(t in blob for t in sleeveless_tokens)
    expects_sleeves = (
        bool(is_outerwear)
        or any(t in blob for t in sleeve_tokens)
        or overall_like
        or bool(ns.get("outer_url"))
    ) and not explicitly_sleeveless

    expects_long_body = (
        overall_like
        or any(t in blob for t in long_body_tokens)
        or ("kurta set" in blob)
    )

    expects_modest_side_coverage = (
        bool(is_outerwear)
        or expects_long_body
        or any(t in blob for t in modest_side_tokens)
    )

    return {
        "blob": blob,
        "overall_like": bool(overall_like),
        "expects_sleeves": bool(expects_sleeves),
        "expects_long_body": bool(expects_long_body),
        "expects_modest_side_coverage": bool(expects_modest_side_coverage),
        "explicitly_sleeveless": bool(explicitly_sleeveless),
    }


# -----------------------------------------------------------------------------
# Non-saree QC (STRICTER + PER-IMAGE)
# -----------------------------------------------------------------------------


class NonSareeQC:
    """
    Non-saree QC is intentionally conservative:
      - every generated image is evaluated individually
      - strict pass means "shippable"
      - hard pass is only used if fail_open is explicitly enabled
      - if nothing is shippable, caller should return zero outputs
    """

    def __init__(self) -> None:
        self.enabled = _env_bool("COMMERCE_VTON_QC_ENABLE", default=True)
        self.fail_open = _env_bool("COMMERCE_VTON_QC_FAIL_OPEN", default=False)

        self.timeout_s = _clamp_int(_env_str("COMMERCE_VTON_QC_TIMEOUT_S", "25"), default=25, lo=5, hi=120)
        self.image_size = _clamp_int(_env_str("COMMERCE_VTON_QC_IMAGE_SIZE", "256"), default=256, lo=96, hi=512)

        # Base strict thresholds
        self.min_upper_diff = _env_float("COMMERCE_VTON_QC_MIN_UPPER_DIFF", 0.03)
        self.min_lower_diff = _env_float("COMMERCE_VTON_QC_MIN_LOWER_DIFF", 0.03)
        self.min_full_diff = _env_float("COMMERCE_VTON_QC_MIN_FULL_DIFF", 0.02)
        self.max_face_diff = _env_float("COMMERCE_VTON_QC_MAX_FACE_DIFF", 0.12)

        # Base hard thresholds
        self.hard_min_upper_diff = _env_float("COMMERCE_VTON_QC_HARD_MIN_UPPER_DIFF", 0.015)
        self.hard_min_lower_diff = _env_float("COMMERCE_VTON_QC_HARD_MIN_LOWER_DIFF", 0.015)
        self.hard_min_full_diff = _env_float("COMMERCE_VTON_QC_HARD_MIN_FULL_DIFF", 0.010)
        self.hard_max_face_diff = _env_float("COMMERCE_VTON_QC_HARD_MAX_FACE_DIFF", 0.20)

        self.outer_face_bonus = _env_float("COMMERCE_VTON_QC_OUTER_FACE_BONUS", 0.05)
        self.min_stddev = _env_float("COMMERCE_VTON_QC_MIN_STDDEV", 0.015)

        # Prior-aware strict thresholds
        self.sleeve_shoulder_min_diff = _env_float("COMMERCE_VTON_QC_SLEEVE_SHOULDER_MIN_DIFF", 0.012)
        self.sleeve_arm_min_diff = _env_float("COMMERCE_VTON_QC_SLEEVE_ARM_MIN_DIFF", 0.010)
        self.side_torso_min_diff = _env_float("COMMERCE_VTON_QC_SIDE_TORSO_MIN_DIFF", 0.012)
        self.long_body_min_diff = _env_float("COMMERCE_VTON_QC_LONG_BODY_MIN_DIFF", 0.014)

        # Prior-aware hard thresholds
        self.hard_sleeve_shoulder_min_diff = _env_float("COMMERCE_VTON_QC_HARD_SLEEVE_SHOULDER_MIN_DIFF", 0.007)
        self.hard_sleeve_arm_min_diff = _env_float("COMMERCE_VTON_QC_HARD_SLEEVE_ARM_MIN_DIFF", 0.006)
        self.hard_side_torso_min_diff = _env_float("COMMERCE_VTON_QC_HARD_SIDE_TORSO_MIN_DIFF", 0.007)
        self.hard_long_body_min_diff = _env_float("COMMERCE_VTON_QC_HARD_LONG_BODY_MIN_DIFF", 0.008)

    async def evaluate(
        self,
        *,
        garment_type: str,
        human_url: str,
        out_url: str,
        is_outerwear: bool = False,
        style_priors: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "qc_enabled": False,
                "ok_strict": True,
                "ok_hard": True,
                "score": 1.0,
                "reject_reasons_strict": [],
                "reject_reasons_hard": [],
            }

        from PIL import Image, ImageChops, ImageStat
        import io

        size = int(self.image_size)
        timeout_s = int(self.timeout_s)
        priors = _as_dict(style_priors)

        def _fetch(url: str) -> Image.Image:
            req = Request(url, headers={"User-Agent": "df-vton-qc"})
            raw = urlopen(req, timeout=timeout_s).read()
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            return im.resize((size, size))

        def _roi(im: Image.Image, x0: float, x1: float, y0: float, y1: float) -> Image.Image:
            W, H = im.size
            box = (int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))
            return im.crop(box)

        def _roi_diff(a: Image.Image, b: Image.Image, x0: float, x1: float, y0: float, y1: float) -> float:
            da = _roi(a, x0, x1, y0, y1)
            db = _roi(b, x0, x1, y0, y1)
            d = ImageChops.difference(da, db)
            st = ImageStat.Stat(d)
            mean = float(sum(st.mean) / max(1.0, float(len(st.mean))))
            return mean / 255.0

        def _stddev_norm(im: Image.Image) -> float:
            st = ImageStat.Stat(im)
            sd = float(sum(st.stddev) / max(1.0, float(len(st.stddev))))
            return sd / 255.0

        human = await asyncio.to_thread(_fetch, human_url)
        out = await asyncio.to_thread(_fetch, out_url)

        face = _roi_diff(human, out, 0.34, 0.66, 0.02, 0.28)
        upper = _roi_diff(human, out, 0.10, 0.90, 0.22, 0.58)
        lower = _roi_diff(human, out, 0.10, 0.90, 0.58, 0.98)
        full = _roi_diff(human, out, 0.08, 0.92, 0.18, 0.98)

        shoulder_left = _roi_diff(human, out, 0.06, 0.28, 0.20, 0.36)
        shoulder_right = _roi_diff(human, out, 0.72, 0.94, 0.20, 0.36)
        arm_left = _roi_diff(human, out, 0.02, 0.22, 0.28, 0.70)
        arm_right = _roi_diff(human, out, 0.78, 0.98, 0.28, 0.70)
        side_left = _roi_diff(human, out, 0.08, 0.24, 0.32, 0.72)
        side_right = _roi_diff(human, out, 0.76, 0.92, 0.32, 0.72)
        lower_mid = _roi_diff(human, out, 0.24, 0.76, 0.52, 0.84)
        center_torso = _roi_diff(human, out, 0.28, 0.72, 0.28, 0.66)

        out_std = _stddev_norm(out)
        blankish = out_std < float(self.min_stddev)

        gt = (garment_type or "").strip().lower() or "unknown"

        strict_max_face = float(self.max_face_diff) + (float(self.outer_face_bonus) if is_outerwear else 0.0)
        hard_max_face = float(self.hard_max_face_diff) + (float(self.outer_face_bonus) if is_outerwear else 0.0)

        strict_reasons: List[str] = []
        hard_reasons: List[str] = []

        if blankish:
            strict_reasons.append("blankish_output")
            hard_reasons.append("blankish_output")

        if gt == "upper_body":
            main_change = upper
            if upper < float(self.min_upper_diff):
                strict_reasons.append("upper_change_too_low")
            if upper < float(self.hard_min_upper_diff):
                hard_reasons.append("upper_change_too_low")
        elif gt == "lower_body":
            main_change = lower
            if lower < float(self.min_lower_diff):
                strict_reasons.append("lower_change_too_low")
            if lower < float(self.hard_min_lower_diff):
                hard_reasons.append("lower_change_too_low")
        else:
            main_change = full
            if full < float(self.min_full_diff):
                strict_reasons.append("full_change_too_low")
            if full < float(self.hard_min_full_diff):
                hard_reasons.append("full_change_too_low")

        if face > strict_max_face:
            strict_reasons.append("face_drift_too_high")
        if face > hard_max_face:
            hard_reasons.append("face_drift_too_high")

        expects_sleeves = bool(priors.get("expects_sleeves"))
        expects_long_body = bool(priors.get("expects_long_body"))
        expects_modest_side_coverage = bool(priors.get("expects_modest_side_coverage"))

        shoulder_cov = max(float(shoulder_left), float(shoulder_right))
        arm_cov = (float(arm_left) + float(arm_right)) / 2.0
        side_cov = min(float(side_left), float(side_right))

        if expects_sleeves:
            if shoulder_cov < float(self.sleeve_shoulder_min_diff) and arm_cov < float(self.sleeve_arm_min_diff):
                strict_reasons.append("missing_sleeve_coverage")
            if shoulder_cov < float(self.hard_sleeve_shoulder_min_diff) and arm_cov < float(self.hard_sleeve_arm_min_diff):
                hard_reasons.append("missing_sleeve_coverage")

        if expects_modest_side_coverage:
            if side_cov < float(self.side_torso_min_diff):
                strict_reasons.append("side_torso_coverage_too_low")
            if side_cov < float(self.hard_side_torso_min_diff):
                hard_reasons.append("side_torso_coverage_too_low")

        if expects_long_body:
            if lower_mid < float(self.long_body_min_diff):
                strict_reasons.append("long_body_coverage_too_low")
            if lower_mid < float(self.hard_long_body_min_diff):
                hard_reasons.append("long_body_coverage_too_low")

        ok_strict = len(strict_reasons) == 0
        ok_hard = len(hard_reasons) == 0

        score = float(main_change) - 0.70 * float(face)
        score += 0.25 * min(0.08, shoulder_cov)
        score += 0.20 * min(0.08, arm_cov)
        score += 0.25 * min(0.08, side_cov)
        score += 0.25 * min(0.08, float(lower_mid))
        score += 0.10 * min(0.08, float(center_torso))

        if blankish:
            score -= 1.0
        if strict_reasons:
            score -= 0.15 * float(len(strict_reasons))
        elif hard_reasons:
            score -= 0.05 * float(len(hard_reasons))

        return {
            "qc_enabled": True,
            "ok_strict": bool(ok_strict),
            "ok_hard": bool(ok_hard),
            "score": float(score),
            "garment_type": gt,
            "is_outerwear": bool(is_outerwear),
            "style_priors": {
                "expects_sleeves": expects_sleeves,
                "expects_long_body": expects_long_body,
                "expects_modest_side_coverage": expects_modest_side_coverage,
            },
            "face": float(face),
            "upper": float(upper),
            "lower": float(lower),
            "full": float(full),
            "shoulder_left": float(shoulder_left),
            "shoulder_right": float(shoulder_right),
            "arm_left": float(arm_left),
            "arm_right": float(arm_right),
            "side_left": float(side_left),
            "side_right": float(side_right),
            "lower_mid": float(lower_mid),
            "center_torso": float(center_torso),
            "out_stddev": float(out_std),
            "blankish": bool(blankish),
            "reject_reasons_strict": strict_reasons,
            "reject_reasons_hard": hard_reasons,
            "thresholds": {
                "strict": {
                    "min_upper_diff": float(self.min_upper_diff),
                    "min_lower_diff": float(self.min_lower_diff),
                    "min_full_diff": float(self.min_full_diff),
                    "max_face_diff": float(self.max_face_diff),
                    "sleeve_shoulder_min_diff": float(self.sleeve_shoulder_min_diff),
                    "sleeve_arm_min_diff": float(self.sleeve_arm_min_diff),
                    "side_torso_min_diff": float(self.side_torso_min_diff),
                    "long_body_min_diff": float(self.long_body_min_diff),
                    "outer_face_bonus": float(self.outer_face_bonus),
                    "min_stddev": float(self.min_stddev),
                },
                "hard": {
                    "min_upper_diff": float(self.hard_min_upper_diff),
                    "min_lower_diff": float(self.hard_min_lower_diff),
                    "min_full_diff": float(self.hard_min_full_diff),
                    "max_face_diff": float(self.hard_max_face_diff),
                    "sleeve_shoulder_min_diff": float(self.hard_sleeve_shoulder_min_diff),
                    "sleeve_arm_min_diff": float(self.hard_sleeve_arm_min_diff),
                    "side_torso_min_diff": float(self.hard_side_torso_min_diff),
                    "long_body_min_diff": float(self.hard_long_body_min_diff),
                    "outer_face_bonus": float(self.outer_face_bonus),
                    "min_stddev": float(self.min_stddev),
                },
            },
            "computed": {
                "strict_max_face": float(strict_max_face),
                "hard_max_face": float(hard_max_face),
                "shoulder_cov": float(shoulder_cov),
                "arm_cov": float(arm_cov),
                "side_cov": float(side_cov),
            },
        }


# -----------------------------------------------------------------------------
# Saree Providers (UNCHANGED)
# -----------------------------------------------------------------------------


class SareeLoRAProvider:
    """
    ML-first saree try-on using a trained edit LoRA via fal-ai/flux-2/lora/edit.
    """

    def __init__(self, *, fal: FalQueueClient) -> None:
        self.fal = fal
        self.enabled = _env_bool("COMMERCE_ENABLE_SAREE_TRYON_PROVIDER", False) or _env_bool("DF_ENABLE_SAREE_TRYON_PROVIDER", False)
        self.endpoint_id = (_env_str("COMMERCE_SAREE_LORA_ENDPOINT_ID", "fal-ai/flux-2/lora/edit") or "fal-ai/flux-2/lora/edit").strip().strip("/")
        self.lora_url = (_env_str("DF_SAREE_TRAINED_LORA_URL", "") or _env_str("COMMERCE_SAREE_TRAINED_LORA_URL", "")).strip()
        self.lora_scale = _coerce_float(_env_str("DF_SAREE_TRAINED_LORA_SCALE", "1.1"), 1.1)

        self.output_format = (_env_str("COMMERCE_SAREE_LORA_OUTPUT_FORMAT", "png") or "png").strip().lower()
        if self.output_format not in ("png", "jpeg", "jpg", "webp"):
            self.output_format = "png"
        if self.output_format == "jpg":
            self.output_format = "jpeg"

        self.num_images = _clamp_int(_env_str("COMMERCE_SAREE_LORA_NUM_IMAGES", "1"), default=1, lo=1, hi=4)

        self.prompt = _env_str(
            "COMMERCE_SAREE_LORA_PROMPT",
            (
                "Photorealistic full-body photo. Drape a traditional Indian saree in nivi style with realistic pleats and pallu. "
                "Use the garment reference image for the exact saree fabric pattern and colors. "
                "Preserve face identity, body shape, pose, lighting, and background. "
                "Do not change the person. Do not change the scene."
            ),
        )

    def can_run(self) -> bool:
        return bool(self.enabled and self.lora_url)

    async def generate(
        self,
        *,
        human_url: str,
        garment_proxy_url: str,
        saree_ref_url: str,
        seed: int,
    ) -> Tuple[List[str], Dict[str, Any]]:
        if not self.can_run():
            raise RuntimeError("SAREE_LORA_DISABLED_OR_MISSING_LORA_URL")

        input_json: Dict[str, Any] = {
            "prompt": self.prompt,
            "image_urls": [human_url, garment_proxy_url, saree_ref_url],
            "loras": [{"path": self.lora_url, "scale": float(self.lora_scale)}],
            "seed": int(seed),
            "num_images": int(self.num_images),
            "output_format": str(self.output_format),
        }

        out, dbg = await self.fal.run_and_wait(endpoint_id=self.endpoint_id, input_json=input_json)
        urls = _parse_fal_any_image_urls(_as_dict(out))
        if not urls:
            raise RuntimeError(f"SAREE_LORA_NO_OUTPUT_URLS out_keys={list(_as_dict(out).keys())[:40]}")
        return urls, {"fal": dbg, "endpoint_id": self.endpoint_id, "lora_scale": float(self.lora_scale)}


class SareeOverlayFallbackProvider:
    """
    Overlay fallback only (Blender-based).
    """

    def __init__(self) -> None:
        self.enabled = _env_bool("COMMERCE_ENABLE_SAREE_DRAPE_PROVIDER", False) or _env_bool("DF_ENABLE_SAREE_DRAPE_PROVIDER", False)
        self._impl: Optional[Any] = None

    def _get_impl(self) -> Any:
        if self._impl is not None:
            return self._impl

        from app.services.azure_storage_service import AzureStorageService
        from app.services.drape.blender_runner import BlenderRunner
        from app.services.providers.saree_drape_provider import SareeDrapeProvider

        storage = AzureStorageService()
        runner = BlenderRunner()
        self._impl = _construct_with_supported_kwargs(SareeDrapeProvider, storage=storage, blender_runner=runner, runner=runner, config=None)
        return self._impl

    async def run_overlay(
        self,
        *,
        req: VTONGenerateRequest,
        human_url: str,
        saree_ref_url: str,
        drape_style: str,
        variant_idx: int,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("SAREE_OVERLAY_DISABLED")

        impl = self._get_impl()

        items = _as_list(_as_dict(req.product_assets).get("items"))
        if not items:
            items = [{"component_code": "saree", "name": "saree", "image_url": saree_ref_url}]

        request_env = {
            "input": {"product_assets": {"items": items}, "views": {"full_body": True}, "model_ref": {"url": human_url}},
            "product_assets": {"items": items},
            "model_ref": {"url": human_url, "full_body": True},
            "items": items,
        }
        resolved_inputs = {
            "outfit_kind": "saree_set",
            "saree_like": True,
            "drape_style": (drape_style or "nivi").strip().lower(),
            "items": items,
            "product_assets": {"items": items},
            "views": {"full_body": True},
            "model_ref": {"url": human_url, "full_body": True},
            "saree_url": saree_ref_url,
        }

        out = await asyncio.to_thread(
            impl.run,
            job_id=f"{req.studio_job_id}-{variant_idx}",
            user_id=req.user_id,
            request=request_env,
            resolved_inputs=resolved_inputs,
        )
        return _as_dict(out)


# -----------------------------------------------------------------------------
# Non-saree providers
# -----------------------------------------------------------------------------


class ImageAppsV2TryOnProvider:
    """
    fal-ai/image-apps-v2/virtual-try-on
    Inputs: person_image_url, clothing_image_url, preserve_pose, aspect_ratio (dict)
    """

    def __init__(self, *, fal: FalQueueClient) -> None:
        self.fal = fal
        self.enabled = _env_bool("COMMERCE_ENABLE_IMAGEAPPS_V2_VTON", default=False) or _env_bool("DF_ENABLE_IMAGEAPPS_V2_VTON", default=False)
        self.endpoint_id = (_env_str("COMMERCE_IMAGEAPPS_V2_VTON_ENDPOINT_ID", "fal-ai/image-apps-v2/virtual-try-on") or "fal-ai/image-apps-v2/virtual-try-on").strip().strip("/")
        self.preserve_pose = _env_bool("COMMERCE_IMAGEAPPS_V2_PRESERVE_POSE", default=True)
        self.aspect_ratio = (_env_str("COMMERCE_IMAGEAPPS_V2_ASPECT_RATIO", "3:4") or "3:4").strip()

    def can_run(self) -> bool:
        return bool(self.enabled)

    async def generate_one(self, *, person_url: str, clothing_url: str) -> Tuple[str, Dict[str, Any]]:
        if not self.can_run():
            raise RuntimeError("IMAGEAPPS_V2_DISABLED")

        inp: Dict[str, Any] = {
            "person_image_url": str(person_url),
            "clothing_image_url": str(clothing_url),
            "preserve_pose": bool(self.preserve_pose),
            "aspect_ratio": _imageapps_aspect_ratio_obj(self.aspect_ratio),
        }

        out, dbg = await self.fal.run_and_wait(endpoint_id=self.endpoint_id, input_json=inp)
        urls = _parse_fal_any_image_urls(_as_dict(out))
        if not urls:
            raise RuntimeError(f"IMAGEAPPS_V2_NO_OUTPUT_URLS out_keys={list(_as_dict(out).keys())[:40]}")
        return urls[0], {"fal": dbg, "endpoint_id": self.endpoint_id, "aspect_ratio": inp["aspect_ratio"]}


class CatVTONProvider:
    """
    fal-ai/cat-vton
    Inputs: human_image_url, garment_image_url, cloth_type, ...
    """

    def __init__(self, *, fal: FalQueueClient) -> None:
        self.fal = fal
        self.enabled = _env_bool("COMMERCE_ENABLE_CATVTON", default=False) or _env_bool("DF_ENABLE_CATVTON", default=False)
        self.endpoint_id = (_env_str("COMMERCE_CATVTON_ENDPOINT_ID", "fal-ai/cat-vton") or "fal-ai/cat-vton").strip().strip("/")
        self.num_inference_steps = _clamp_int(_env_str("COMMERCE_CATVTON_STEPS", "30"), default=30, lo=10, hi=60)
        self.guidance_scale = _coerce_float(_env_str("COMMERCE_CATVTON_GUIDANCE", "2.5"), 2.5)

    def can_run(self) -> bool:
        return bool(self.enabled)

    async def generate_one(self, *, human_url: str, garment_url: str, cloth_type: str) -> Tuple[str, Dict[str, Any]]:
        if not self.can_run():
            raise RuntimeError("CATVTON_DISABLED")

        inp: Dict[str, Any] = {
            "human_image_url": str(human_url),
            "garment_image_url": str(garment_url),
            "cloth_type": str(cloth_type or "overall"),
            "num_inference_steps": int(self.num_inference_steps),
            "guidance_scale": float(self.guidance_scale),
        }

        out, dbg = await self.fal.run_and_wait(endpoint_id=self.endpoint_id, input_json=inp)
        urls = _parse_fal_any_image_urls(_as_dict(out))
        if not urls:
            raise RuntimeError(f"CATVTON_NO_OUTPUT_URLS out_keys={list(_as_dict(out).keys())[:40]}")
        return urls[0], {"fal": dbg, "endpoint_id": self.endpoint_id, "cloth_type": str(cloth_type or "overall")}


def _catvton_cloth_type_for(*, garment_type: str, has_outer: bool = False) -> str:
    gt = (garment_type or "").strip().lower()
    if has_outer:
        return "outer"
    if gt == "upper_body":
        return "upper"
    if gt == "lower_body":
        return "lower"
    return "overall"


# -----------------------------------------------------------------------------
# Main provider router
# -----------------------------------------------------------------------------


class VTONProvider:
    """
    Key non-saree behavior:
      - evaluate every generated output individually
      - only return top shippable result(s)
      - return [] when nothing is shippable
    """

    def __init__(self) -> None:
        self.enable_real = _env_bool("COMMERCE_ENABLE_REAL_PROVIDERS", default=False)
        self.provider = (_env_str("COMMERCE_VTON_PROVIDER", "fal") or "fal").strip().lower()

        self.saree_strict = _env_bool("COMMERCE_SAREE_STRICT", default=True)
        self.enforce_full_body_for_saree = _env_bool("COMMERCE_ENFORCE_FULL_BODY_FOR_SAREE", default=True)

        self.max_provider_images = _clamp_int(_env_str("COMMERCE_MAX_PROVIDER_IMAGES", "4"), default=4, lo=1, hi=12)

        # Non-saree final-return policy
        self.max_returned_images = _clamp_int(_env_str("COMMERCE_VTON_MAX_RETURNED_IMAGES", "2"), default=2, lo=1, hi=4)
        self.min_top_strict_score = _env_float("COMMERCE_VTON_MIN_TOP_STRICT_SCORE", 0.0)
        self.min_second_strict_score = _env_float("COMMERCE_VTON_MIN_SECOND_STRICT_SCORE", 0.015)
        self.max_second_score_gap = _env_float("COMMERCE_VTON_MAX_SECOND_SCORE_GAP", 0.020)
        self.min_top_hard_score = _env_float("COMMERCE_VTON_MIN_TOP_HARD_SCORE", 0.010)
        self.min_second_hard_score = _env_float("COMMERCE_VTON_MIN_SECOND_HARD_SCORE", 0.020)

        # Fal endpoints
        self.fal_fashn_endpoint_id = (_env_str("COMMERCE_FAL_FASHN_ENDPOINT_ID", "fal-ai/fashn/tryon/v1.6") or "fal-ai/fashn/tryon/v1.6").strip().strip("/")
        self.fashn_mode = (_env_str("COMMERCE_FASHN_MODE", "quality") or "quality").strip().lower()
        if self.fashn_mode not in ("performance", "balanced", "quality"):
            self.fashn_mode = "quality"

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

        self.non_saree_provider_order = [
            p.strip().lower()
            for p in (_env_str("COMMERCE_NON_SAREE_PROVIDER_ORDER", "imageapps_v2,fashn,catvton") or "imageapps_v2,fashn,catvton").split(",")
            if p.strip()
        ]

        self.two_piece_sequential = _env_bool("COMMERCE_VTON_TWO_PIECE_SEQUENTIAL", default=False)

        self.indian_non_saree_strict = _env_bool("COMMERCE_INDIAN_NON_SAREE_STRICT", default=True)
        self.enable_indian_non_saree_generic_fallback = (
            _env_bool("COMMERCE_ENABLE_INDIAN_NON_SAREE_GENERIC_FALLBACK", default=False)
            or _env_bool("COMMERCE_ALLOW_GENERIC_INDIAN_NON_SAREE_FALLBACK", default=False)
            or _env_bool("DF_ENABLE_INDIAN_NON_SAREE_GENERIC_FALLBACK", default=False)
            or _env_bool("DF_ALLOW_GENERIC_INDIAN_NON_SAREE_GENERIC_FALLBACK", default=False)
            or _env_bool("DF_ALLOW_GENERIC_INDIAN_NON_SAREE_FALLBACK", default=False)
        )
        self.supported_non_saree_providers = set(_SUPPORTED_NON_SAREE_PROVIDERS)

        self.enable_platform_model_selector = _env_bool("COMMERCE_ENABLE_PLATFORM_MODEL_SELECTOR", default=True)
        self.platform_model_force_when_missing_human = _env_bool("COMMERCE_PLATFORM_MODEL_FORCE_WHEN_MISSING_HUMAN", default=True)
        self.platform_model_top_k = _clamp_int(_env_str("COMMERCE_PLATFORM_MODELS_TOP_K", "10"), default=10, lo=1, hi=50)
        self._platform_selector: Optional[Any] = None
        self._platform_asset_url_cache: Dict[str, str] = {}

        self.fal = FalQueueClient()
        self.saree_lora = SareeLoRAProvider(fal=self.fal)
        self.saree_overlay = SareeOverlayFallbackProvider()
        self.saree_qc = SareeQC()

        self.nonsaree_qc = NonSareeQC()
        self.imageapps_v2 = ImageAppsV2TryOnProvider(fal=self.fal)
        self.catvton = CatVTONProvider(fal=self.fal)

        self.allow_placeholder_fallback = _env_bool("COMMERCE_ALLOW_PLACEHOLDER_FALLBACK", default=False)
        self.placeholder_base = (_env_str("COMMERCE_PLACEHOLDER_BASE", "https://placehold.co") or "https://placehold.co").rstrip("/")

        self.output_container = (_env_str("COMMERCE_OUTPUT_CONTAINER", "commerce-output") or "commerce-output").strip()
        self.output_prefix_base = (_env_str("COMMERCE_VTON_OUTPUT_PREFIX", "commerce/vton") or "commerce/vton").strip().strip("/")
        self.materialize_to_azure = _env_bool("COMMERCE_VTON_MATERIALIZE_TO_AZURE", default=True)
        self.materialize_timeout_s = _clamp_int(_env_str("COMMERCE_VTON_MATERIALIZE_TIMEOUT_S", "180"), default=180, lo=30, hi=600)
        self.sas_expires_in_s = _clamp_int(_env_str("COMMERCE_VTON_SAS_EXPIRES_S", "86400"), default=86400, lo=600, hi=7 * 86400)

    def _placeholder_url(self, *, product_type: str, pose: str, bg: str, idx: int) -> str:
        txt = f"vton+{product_type}+{pose}+{bg}+{idx}"
        return f"{self.placeholder_base}/1024x1024/png?text={txt}"

    def _fal_key(self) -> str:
        return (_env_str("FAL_KEY", "") or _env_str("FAL_API_KEY", "") or _env_str("COMMERCE_FAL_KEY", "")).strip()

    def _resolve_platform_model_asset_url(self, url: str) -> str:
        if _is_http_url(url):
            return str(url)

        cached = self._platform_asset_url_cache.get(str(url))
        if cached:
            return cached

        az_ref = _parse_az_ref(str(url))
        if not az_ref:
            return str(url)

        storage = _get_storage_service_best_effort()
        container, blob_name = az_ref
        sas_url = _call_any_sas_method(
            storage,
            container=container,
            blob_name=blob_name,
            expires_in_s=int(self.sas_expires_in_s),
            permission="r",
        )
        self._platform_asset_url_cache[str(url)] = sas_url
        return sas_url

    def _get_platform_selector(self) -> Any:
        if self._platform_selector is not None:
            return self._platform_selector

        from app.services.catalog.platform_model_selector import PlatformModelSelector

        self._platform_selector = PlatformModelSelector(
            asset_url_resolver=self._resolve_platform_model_asset_url,
        )
        return self._platform_selector

    def _resolve_non_saree_provider_order(self, *, family: Optional[str]) -> List[str]:
        configured = [p for p in self.non_saree_provider_order if p in self.supported_non_saree_providers]
        if not configured:
            configured = ["imageapps_v2", "fashn", "catvton"]

        fam = _norm_text(family)
        if not fam:
            return configured

        fam_key = re.sub(r"[^A-Za-z0-9]+", "_", fam).strip("_").upper()
        if not fam_key:
            return configured

        raw_override = ""
        for env_name in (
            f"DF_NONSAREE_PROVIDER_{fam_key}",
            f"COMMERCE_NONSAREE_PROVIDER_{fam_key}",
            f"COMMERCE_NON_SAREE_PROVIDER_{fam_key}",
        ):
            raw_override = (os.getenv(env_name) or "").strip()
            if raw_override:
                break

        if not raw_override:
            return configured

        requested = _csv_tokens(raw_override)
        if not requested:
            return configured

        if any(p in ("auto", "default", "global") for p in requested):
            return configured

        valid: List[str] = []
        invalid: List[str] = []

        for p in requested:
            if p in self.supported_non_saree_providers:
                if p not in valid:
                    valid.append(p)
            else:
                invalid.append(p)

        if invalid:
            logger.warning(
                "Ignoring unsupported non-saree provider override family=%s override=%s invalid=%s supported=%s",
                fam,
                raw_override,
                invalid,
                sorted(self.supported_non_saree_providers),
            )

        if not valid:
            return configured

        out = list(valid)
        for p in configured:
            if p not in out:
                out.append(p)
        return out

    async def _select_platform_model_for_non_saree(
        self,
        *,
        req: VTONGenerateRequest,
        product_assets: Dict[str, Any],
        model_ref: Dict[str, Any],
        garment_kind: str,
    ) -> Dict[str, Any]:
        selector = self._get_platform_selector()

        pa_meta = _as_dict(product_assets.get("meta"))
        mr_meta = _as_dict(model_ref.get("meta"))

        tenantish = str(
            product_assets.get("tenant_id")
            or pa_meta.get("tenant_id")
            or model_ref.get("tenant_id")
            or mr_meta.get("tenant_id")
            or req.user_id
        )

        product_id = (
            product_assets.get("product_id")
            or pa_meta.get("product_id")
            or model_ref.get("product_id")
            or mr_meta.get("product_id")
        )

        preferred_tags = _resolve_platform_preferred_tags(product_assets=product_assets, model_ref=model_ref)
        recent_model_codes = _resolve_recent_platform_model_codes(product_assets=product_assets, model_ref=model_ref)

        return selector.select_platform_model(
            garment_kind=garment_kind,
            tenant_id=str(tenantish),
            quote_id=str(req.quote_id),
            product_id=str(product_id) if product_id else None,
            preferred_tags=preferred_tags,
            recent_model_codes=recent_model_codes,
            top_k=int(self.platform_model_top_k),
        )

    async def _build_saree_garment_proxy_url(
        self,
        *,
        user_id: UUID,
        job_id: UUID,
        saree_ref_url: str,
    ) -> Tuple[str, Dict[str, Any]]:
        from app.services.azure_storage_service import AzureStorageService
        from app.services.providers.saree_drape_provider import SareeDrapeProvider

        storage = AzureStorageService()
        impl = _construct_with_supported_kwargs(SareeDrapeProvider, storage=storage, blender_runner=None, runner=None, config=None)

        proxy_url, proxy_dbg = await asyncio.to_thread(
            impl.build_garment_proxy_url,
            user_id=str(user_id),
            job_id=str(job_id),
            saree_url=saree_ref_url,
        )
        return proxy_url, proxy_dbg

    async def _materialize_variant_urls(
        self,
        *,
        req: VTONGenerateRequest,
        route: str,
        provider_tag: str,
        source_urls: List[str],
        expand_to_requested_count: bool = False,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        If expand_to_requested_count=True, replicate last source URL until len(req.variants).
        Otherwise materialize exactly len(source_urls).
        """
        if not source_urls:
            return [], {"materialized": False, "reason": "no_source_urls"}

        source_urls_norm = list(source_urls)
        if expand_to_requested_count and len(source_urls_norm) < len(req.variants):
            source_urls_norm = list(source_urls_norm) + [source_urls_norm[-1]] * (len(req.variants) - len(source_urls_norm))

        if not self.materialize_to_azure:
            return source_urls_norm, {"materialized": False}

        storage = _get_storage_service_best_effort()
        container = self.output_container

        out_urls: List[str] = []
        debug: List[Dict[str, Any]] = []

        for i, src in enumerate(source_urls_norm):
            if not _is_http_url(src):
                raise RuntimeError(f"materialize_bad_source_url idx={i} url={src!r}")

            variant_id = _variant_job_id(job_id=req.studio_job_id, variant_index=i)

            if f"/{variant_id}/" in src:
                out_urls.append(src)
                debug.append({"i": i, "variant_job_id": variant_id, "src": src, "dst": src, "kept": True})
                continue

            ext, ct = _guess_ext_and_content_type(src, default_ext=".png")
            short = _sha256(f"{req.request_hash}:{provider_tag}:{i}:{src}")[:32]

            if provider_tag == "flux2":
                fname = f"tryon_flux2_{short}{ext}"
            elif provider_tag == "overlay":
                fname = f"tryon_overlay_{short}{ext}"
            elif provider_tag == "fashn":
                fname = f"tryon_fashn_{short}{ext}"
            elif provider_tag == "imageapps_v2":
                fname = f"tryon_imageapps_v2_{short}{ext}"
            elif provider_tag == "catvton":
                fname = f"tryon_catvton_{short}{ext}"
            else:
                fname = f"tryon_{provider_tag}_{short}{ext}"

            blob_name = f"{self.output_prefix_base}/{route}/{req.user_id}/{variant_id}/{req.request_hash}/{fname}"

            data = await asyncio.to_thread(_download_bytes, src, timeout_s=int(self.materialize_timeout_s))
            if not data:
                raise RuntimeError(f"materialize_download_empty idx={i} url={src}")

            await asyncio.to_thread(
                _call_any_upload_method,
                storage,
                container=container,
                blob_name=blob_name,
                data=data,
                content_type=ct,
            )

            sas_url = await asyncio.to_thread(
                _call_any_sas_method,
                storage,
                container=container,
                blob_name=blob_name,
                expires_in_s=int(self.sas_expires_in_s),
                permission="r",
            )

            out_urls.append(sas_url)
            debug.append({"i": i, "variant_job_id": variant_id, "src": src, "dst": sas_url, "blob": blob_name, "content_type": ct})

        return out_urls, {"materialized": True, "container": container, "route": route, "debug": debug[:8]}

    async def _evaluate_non_saree_provider_outputs(
        self,
        *,
        provider_name: str,
        provider_id: str,
        route: str,
        tag: str,
        urls: List[str],
        base_meta: Dict[str, Any],
        garment_type: str,
        human_url: str,
        is_outerwear: bool,
        style_priors: Dict[str, Any],
    ) -> Dict[str, Any]:
        image_results: List[Dict[str, Any]] = []

        for i, out_url in enumerate(urls):
            qc = await self.nonsaree_qc.evaluate(
                garment_type=garment_type,
                human_url=human_url,
                out_url=out_url,
                is_outerwear=is_outerwear,
                style_priors=style_priors,
            )
            image_results.append(
                {
                    "index": int(i),
                    "url": out_url,
                    "score": float(_as_dict(qc).get("score", -999.0)),
                    "ok_strict": bool(_as_dict(qc).get("ok_strict", False)),
                    "ok_hard": bool(_as_dict(qc).get("ok_hard", False)),
                    "qc": qc,
                }
            )

        strict_images = sorted([x for x in image_results if x["ok_strict"]], key=lambda x: float(x["score"]), reverse=True)
        hard_images = sorted([x for x in image_results if x["ok_hard"]], key=lambda x: float(x["score"]), reverse=True)
        best_image = sorted(image_results, key=lambda x: float(x["score"]), reverse=True)[0] if image_results else None

        meta = dict(base_meta)
        meta.update(
            {
                "provider_name": provider_name,
                "style_priors": {
                    "expects_sleeves": bool(style_priors.get("expects_sleeves")),
                    "expects_long_body": bool(style_priors.get("expects_long_body")),
                    "expects_modest_side_coverage": bool(style_priors.get("expects_modest_side_coverage")),
                },
                "qc_summary": {
                    "image_count": len(image_results),
                    "strict_pass_count": len(strict_images),
                    "hard_pass_count": len(hard_images),
                    "best_score": float(best_image["score"]) if best_image else None,
                    "best_strict_score": float(strict_images[0]["score"]) if strict_images else None,
                    "best_hard_score": float(hard_images[0]["score"]) if hard_images else None,
                },
                "image_results": [
                    {
                        "index": x["index"],
                        "score": x["score"],
                        "ok_strict": x["ok_strict"],
                        "ok_hard": x["ok_hard"],
                        "reject_reasons_strict": _as_dict(x["qc"]).get("reject_reasons_strict", []),
                        "reject_reasons_hard": _as_dict(x["qc"]).get("reject_reasons_hard", []),
                    }
                    for x in image_results[:8]
                ],
            }
        )

        return {
            "provider": provider_id,
            "route": route,
            "tag": tag,
            "meta": meta,
            "image_results": image_results,
            "strict_images": strict_images,
            "hard_images": hard_images,
            "best_image": best_image,
            "best_score": float(best_image["score"]) if best_image else -999.0,
            "best_strict_score": float(strict_images[0]["score"]) if strict_images else -999.0,
            "best_hard_score": float(hard_images[0]["score"]) if hard_images else -999.0,
        }

    def _pick_final_images_from_provider(self, *, chosen: Dict[str, Any], pool_name: str) -> List[Dict[str, Any]]:
        ranked = list(_as_list(chosen.get("strict_images" if pool_name == "strict" else "hard_images")))
        if not ranked:
            return []

        best = ranked[0]
        best_score = float(best.get("score", -999.0))

        if pool_name == "strict":
            if best_score < float(self.min_top_strict_score):
                return []
            min_second_score = float(self.min_second_strict_score)
        else:
            if best_score < float(self.min_top_hard_score):
                return []
            min_second_score = float(self.min_second_hard_score)

        out: List[Dict[str, Any]] = [best]
        seen = {str(best.get("url") or "").strip()}

        if int(self.max_returned_images) <= 1:
            return out

        for cand in ranked[1:]:
            if len(out) >= int(self.max_returned_images):
                break

            url = str(cand.get("url") or "").strip()
            if not url or url in seen:
                continue

            score = float(cand.get("score", -999.0))
            if score < min_second_score:
                continue

            if (best_score - score) > float(self.max_second_score_gap):
                continue

            out.append(cand)
            seen.add(url)

        return out

    def _empty_non_saree_result(
        self,
        *,
        reason: str,
        garment_type: str,
        indian_non_saree_family: Optional[str],
        is_outerwear: bool,
        provider_order: List[str],
        provider_errors: List[Dict[str, Any]],
        provider_candidates: List[Dict[str, Any]],
        resolved: Dict[str, Any],
        platform_model_selection: Optional[Dict[str, Any]],
        use_sequential: bool,
        generic_indian_non_saree_fallback_enabled: bool,
    ) -> VTONGenerateResult:
        return VTONGenerateResult(
            provider="none",
            urls=[],
            meta={
                "route": "non_saree_no_shippable_outputs",
                "reason": reason,
                "garment_type": garment_type,
                "indian_non_saree_family": indian_non_saree_family,
                "is_outerwear": bool(is_outerwear),
                "provider_order": provider_order,
                "provider_order_configured": self.non_saree_provider_order,
                "generic_indian_non_saree_fallback_enabled": generic_indian_non_saree_fallback_enabled,
                "two_piece_sequential": bool(use_sequential),
                "errors": provider_errors[:8],
                "candidates": [
                    {
                        "provider": c.get("provider"),
                        "route": c.get("route"),
                        "best_score": c.get("best_score"),
                        "best_strict_score": c.get("best_strict_score"),
                        "best_hard_score": c.get("best_hard_score"),
                        "qc_summary": _as_dict(c.get("meta")).get("qc_summary"),
                        "image_results": _as_dict(c.get("meta")).get("image_results", []),
                    }
                    for c in provider_candidates[:8]
                ],
                "resolved": resolved,
                "platform_model_selection": platform_model_selection,
                "no_shippable_outputs": True,
            },
        )

    async def generate(self, req: VTONGenerateRequest) -> VTONGenerateResult:
        product_assets = _as_dict(req.product_assets)
        model_ref = _as_dict(req.model_ref)
        product_type = str(product_assets.get("product_type") or "apparel").lower()

        human_url = _resolve_human_image_url(model_ref=model_ref)
        comps = _resolve_outfit_components(product_assets=product_assets)

        garment_url = comps.get("primary_garment_url")
        saree_url = comps.get("saree_url") or garment_url

        if not saree_url:
            raise RuntimeError(
                "VTONProvider: missing garment_image_url. "
                "Provide product_assets.garment_image_url or items[] with dominant_component_code."
            )

        saree_like = _is_saree_like(product_assets=product_assets, garment_url=saree_url)
        garment_type = _infer_garment_type(product_assets=product_assets, garment_url=saree_url)
        default_drape_style = str(product_assets.get("drape_style") or "").strip().lower() or "nivi"
        full_body_flag = _compute_full_body_flag(req=req, model_ref=model_ref)

        if saree_like and not human_url:
            raise RuntimeError(
                "VTONProvider: missing human_image_url for saree flow. "
                "Provide model_ref.url / model_ref.human_image_url."
            )

        urls_placeholders = [
            self._placeholder_url(product_type=product_type, pose=v.pose, bg=v.background, idx=i)
            for i, v in enumerate(req.variants)
        ]
        n_real = min(len(req.variants), self.max_provider_images)

        # -------------------------
        # Saree routing
        # -------------------------
        if saree_like:
            if self.enforce_full_body_for_saree and not full_body_flag:
                shot = str(model_ref.get("shot") or model_ref.get("shot_type") or "").strip().lower()
                raise RuntimeError(
                    "Saree try-on requires a full-body human image. "
                    "Set model_ref.full_body=true (or model_ref.shot='full_body') and provide a head-to-toe image. "
                    f"(shot={shot!r})"
                )

            proxy_url, proxy_dbg = await self._build_saree_garment_proxy_url(
                user_id=req.user_id,
                job_id=req.studio_job_id,
                saree_ref_url=str(saree_url),
            )

            lora_meta: Dict[str, Any] = {"proxy": proxy_dbg}
            if self.saree_lora.can_run() and self.enable_real and self.provider == "fal":
                try:
                    seed0 = (
                        req.variants[0].seed
                        if req.variants and req.variants[0].seed is not None
                        else _stable_seed(req.request_hash, 0)
                    )
                    urls, dbg = await self.saree_lora.generate(
                        human_url=human_url,
                        garment_proxy_url=proxy_url,
                        saree_ref_url=str(saree_url),
                        seed=int(seed0),
                    )

                    ok, qc_dbg = await self.saree_qc.quick_gate(human_url=human_url, out_url=urls[0])
                    lora_meta["qc"] = qc_dbg
                    lora_meta["lora"] = dbg

                    if ok:
                        raw_urls = [urls[0]] * len(req.variants)
                        mat_urls, mat_dbg = await self._materialize_variant_urls(
                            req=req,
                            route="saree_drape",
                            provider_tag="flux2",
                            source_urls=raw_urls,
                            expand_to_requested_count=False,
                        )
                        return VTONGenerateResult(
                            provider="fal_flux2_lora_edit",
                            urls=mat_urls,
                            meta={"route": "saree_lora_first", **lora_meta, "storage": mat_dbg},
                        )

                    raise RuntimeError(f"SAREE_LORA_QC_FAILED qc={qc_dbg}")

                except Exception as e:
                    lora_meta["lora_error"] = f"{type(e).__name__}: {e}"

            else:
                lora_meta["lora_disabled_reason"] = (
                    "missing DF_SAREE_TRAINED_LORA_URL or COMMERCE_ENABLE_SAREE_TRYON_PROVIDER=0"
                )

            if self.saree_overlay.enabled:
                try:
                    out0 = await self.saree_overlay.run_overlay(
                        req=req,
                        human_url=human_url,
                        saree_ref_url=str(saree_url),
                        drape_style=(req.variants[0].drape_style or default_drape_style or "nivi"),
                        variant_idx=0,
                    )
                    baseline_url = str(out0.get("output_url") or out0.get("baseline_url") or "").strip()
                    if not baseline_url.startswith("http"):
                        raise RuntimeError("SAREE_OVERLAY_NO_URL")

                    ok, qc_dbg = await self.saree_qc.quick_gate(human_url=human_url, out_url=baseline_url)
                    if not ok:
                        raise RuntimeError(f"SAREE_OVERLAY_QC_FAILED qc={qc_dbg}")

                    raw_urls = [baseline_url] * len(req.variants)
                    mat_urls, mat_dbg = await self._materialize_variant_urls(
                        req=req,
                        route="saree_drape",
                        provider_tag="overlay",
                        source_urls=raw_urls,
                        expand_to_requested_count=False,
                    )

                    return VTONGenerateResult(
                        provider="saree_overlay_fallback",
                        urls=mat_urls,
                        meta={
                            "route": "saree_overlay_fallback",
                            "lora": lora_meta,
                            "overlay_debug": _as_dict(out0.get("debug")),
                            "qc": qc_dbg,
                            "storage": mat_dbg,
                        },
                    )
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    if self.saree_strict:
                        raise RuntimeError(f"SAREE_FAILED_STRICT lora={lora_meta} overlay_err={err}")
                    mat_urls, mat_dbg = await self._materialize_variant_urls(
                        req=req,
                        route="saree_drape",
                        provider_tag="placeholder",
                        source_urls=urls_placeholders,
                        expand_to_requested_count=False,
                    )
                    return VTONGenerateResult(
                        provider="placeholder_saree_failed",
                        urls=mat_urls,
                        meta={
                            "route": "saree_failed_non_strict",
                            "lora": lora_meta,
                            "overlay_err": err,
                            "storage": mat_dbg,
                        },
                    )

            if self.saree_strict:
                raise RuntimeError(f"SAREE_FAILED_STRICT lora={lora_meta} overlay_disabled=True")

            mat_urls, mat_dbg = await self._materialize_variant_urls(
                req=req,
                route="saree_drape",
                provider_tag="placeholder",
                source_urls=urls_placeholders,
                expand_to_requested_count=False,
            )
            return VTONGenerateResult(
                provider="placeholder_saree_failed",
                urls=mat_urls,
                meta={
                    "route": "saree_failed_non_strict",
                    "lora": lora_meta,
                    "overlay_disabled": True,
                    "storage": mat_dbg,
                },
            )

        # -------------------------
        # Non-saree
        # -------------------------

        if not self.enable_real:
            return VTONGenerateResult(
                provider="none",
                urls=[],
                meta={
                    "route": "non_saree_real_providers_disabled",
                    "note": "COMMERCE_ENABLE_REAL_PROVIDERS is off; returning no outputs",
                    "requested_variants": len(req.variants),
                    "returned_variants": 0,
                },
            )

        if self.provider != "fal":
            raise RuntimeError(f"VTONProvider: unsupported provider={self.provider!r} (only 'fal' is implemented here)")

        fal_key = self._fal_key()
        if not fal_key:
            return VTONGenerateResult(
                provider="none",
                urls=[],
                meta={
                    "route": "non_saree_missing_provider_key",
                    "error": "missing FAL_KEY (or FAL_API_KEY / COMMERCE_FAL_KEY)",
                    "requested_variants": len(req.variants),
                    "returned_variants": 0,
                },
            )

        ns = _resolve_non_saree_garments(product_assets=product_assets, comps=comps)
        primary_url = ns.get("primary_url") or garment_url
        upper_url = ns.get("upper_url")
        lower_url = ns.get("lower_url")
        outer_url = ns.get("outer_url")

        indian_routing = resolve_indian_non_saree_routing(
            product_assets=product_assets,
            ns=ns,
            garment_type=garment_type,
            primary_url=primary_url,
            strict=bool(self.indian_non_saree_strict),
            enable_generic_fallback=bool(self.enable_indian_non_saree_generic_fallback),
        )

        indian_non_saree_family = indian_routing.family
        garment_type = indian_routing.runtime_garment_type
        resolved_platform_garment_kind = indian_routing.platform_garment_kind
        generic_indian_non_saree_fallback_enabled = bool(self.enable_indian_non_saree_generic_fallback)

        if indian_routing.requires_dedicated_pipeline:
            if not generic_indian_non_saree_fallback_enabled:
                raise RuntimeError(build_required_pipeline_error(indian_routing))

            logger.warning(
                "INDIAN_NON_SAREE_GENERIC_FALLBACK_OVERRIDE family=%s route=%s runtime_garment_type=%s platform_garment_kind=%s",
                indian_non_saree_family,
                getattr(indian_routing, "route", None),
                garment_type,
                resolved_platform_garment_kind,
            )

        platform_model_selection: Optional[Dict[str, Any]] = None
        platform_mode_requested = _non_saree_platform_mode_requested(
            product_assets=product_assets,
            model_ref=model_ref,
        )

        if self.enable_platform_model_selector and resolved_platform_garment_kind and (
            platform_mode_requested or (self.platform_model_force_when_missing_human and not human_url)
        ):
            try:
                platform_model_selection = await self._select_platform_model_for_non_saree(
                    req=req,
                    product_assets=product_assets,
                    model_ref=model_ref,
                    garment_kind=str(resolved_platform_garment_kind),
                )
                human_url = str(platform_model_selection.get("primary_asset_url") or "").strip() or human_url
            except Exception as e:
                if platform_mode_requested or not human_url:
                    raise RuntimeError(
                        f"PLATFORM_MODEL_SELECTION_FAILED garment_kind={resolved_platform_garment_kind} "
                        f"err={type(e).__name__}: {e}"
                    ) from e
                logger.exception("Platform model selection failed; falling back to provided human_url")

        if not _is_http_url(primary_url):
            raise RuntimeError("VTONProvider: non-saree missing primary garment URL (garment_image_url / items[])")

        if not human_url:
            raise RuntimeError(
                "VTONProvider: non-saree missing human_image_url and no platform model was selected. "
                "Provide model_ref.url/human_image_url or enable platform-model selection with a valid manifest."
            )

        is_outerwear = bool(
            indian_routing.treat_as_outerwear
            or _is_http_url(outer_url)
            or _looks_outerwear_url(primary_url)
            or _looks_outerwear_url(product_assets.get("title"))
            or _looks_outerwear_url(product_assets.get("name"))
        )

        style_priors = _infer_non_saree_style_priors(
            product_assets=product_assets,
            ns=ns,
            garment_type=garment_type,
            is_outerwear=is_outerwear,
        )

        use_sequential = bool(
            (not is_outerwear)
            and (not is_phase1_indian_non_saree_family(indian_non_saree_family))
            and self.two_piece_sequential
            and _is_http_url(upper_url)
            and _is_http_url(lower_url)
        )

        provider_errors: List[Dict[str, Any]] = []
        provider_candidates: List[Dict[str, Any]] = []

        async def _attempt_imageapps_v2() -> Dict[str, Any]:
            if not self.imageapps_v2.can_run():
                raise RuntimeError("IMAGEAPPS_V2_DISABLED")

            urls: List[str] = []
            dbg: List[Dict[str, Any]] = []
            clothing_primary = str(outer_url) if (is_outerwear and _is_http_url(outer_url)) else str(primary_url)

            for i in range(n_real):
                base_person = human_url
                if use_sequential:
                    u1, d1 = await self.imageapps_v2.generate_one(person_url=base_person, clothing_url=str(upper_url))
                    u2, d2 = await self.imageapps_v2.generate_one(person_url=u1, clothing_url=str(lower_url))
                    urls.append(u2)
                    dbg.append({"i": i, "seq": True, "upper": d1, "lower": d2})
                else:
                    u, d = await self.imageapps_v2.generate_one(person_url=base_person, clothing_url=clothing_primary)
                    urls.append(u)
                    dbg.append({"i": i, "seq": False, "dbg": d})

            return await self._evaluate_non_saree_provider_outputs(
                provider_name="imageapps_v2",
                provider_id="fal_imageapps_v2",
                route="imageapps_v2",
                tag="imageapps_v2",
                urls=urls,
                base_meta={
                    "provider": "imageapps_v2",
                    "debug": dbg[:5],
                    "sequential": bool(use_sequential),
                },
                garment_type=garment_type,
                human_url=human_url,
                is_outerwear=is_outerwear,
                style_priors=style_priors,
            )

        async def _attempt_catvton() -> Dict[str, Any]:
            if not self.catvton.can_run():
                raise RuntimeError("CATVTON_DISABLED")

            urls: List[str] = []
            dbg: List[Dict[str, Any]] = []

            clothing_primary = str(outer_url) if (is_outerwear and _is_http_url(outer_url)) else str(primary_url)
            cloth_type = _catvton_cloth_type_for(garment_type=garment_type, has_outer=bool(is_outerwear))

            for i in range(n_real):
                base_person = human_url
                if use_sequential:
                    u1, d1 = await self.catvton.generate_one(
                        human_url=base_person,
                        garment_url=str(upper_url),
                        cloth_type="upper",
                    )
                    u2, d2 = await self.catvton.generate_one(
                        human_url=u1,
                        garment_url=str(lower_url),
                        cloth_type="lower",
                    )
                    urls.append(u2)
                    dbg.append({"i": i, "seq": True, "upper": d1, "lower": d2})
                else:
                    u, d = await self.catvton.generate_one(
                        human_url=base_person,
                        garment_url=clothing_primary,
                        cloth_type=cloth_type,
                    )
                    urls.append(u)
                    dbg.append({"i": i, "seq": False, "dbg": d})

            return await self._evaluate_non_saree_provider_outputs(
                provider_name="catvton",
                provider_id="fal_catvton",
                route="catvton",
                tag="catvton",
                urls=urls,
                base_meta={
                    "provider": "catvton",
                    "debug": dbg[:5],
                    "cloth_type": cloth_type,
                    "sequential": bool(use_sequential),
                },
                garment_type=garment_type,
                human_url=human_url,
                is_outerwear=is_outerwear,
                style_priors=style_priors,
            )

        async def _attempt_fashn() -> Dict[str, Any]:
            endpoint_id = self.fal_fashn_endpoint_id
            cat = "auto" if is_outerwear else _fashn_category_for_garment_type(garment_type)

            urls: List[str] = []
            debug: List[Dict[str, Any]] = []
            clothing_primary = str(outer_url) if (is_outerwear and _is_http_url(outer_url)) else str(primary_url)

            if use_sequential:
                for i in range(n_real):
                    seed = (
                        req.variants[i].seed
                        if i < len(req.variants) and req.variants[i].seed is not None
                        else _stable_seed(req.request_hash, i)
                    )

                    fashn_input_1: Dict[str, Any] = {
                        "model_image": human_url,
                        "garment_image": str(upper_url),
                        "category": "tops",
                        "mode": self.fashn_mode,
                        "garment_photo_type": self.fashn_garment_photo_type,
                        "moderation_level": self.fashn_moderation_level,
                        "seed": int(seed),
                        "num_samples": 1,
                        "segmentation_free": bool(self.fashn_segmentation_free),
                        "output_format": self.fashn_output_format,
                    }
                    if self.fashn_sync_mode:
                        fashn_input_1["sync_mode"] = True

                    out1, dbg1 = await self.fal.run_and_wait(endpoint_id=endpoint_id, input_json=fashn_input_1)
                    u1s = _parse_fal_any_image_urls(_as_dict(out1))
                    if not u1s:
                        raise RuntimeError("FASHN_SEQ_STAGE1_NO_URLS")

                    fashn_input_2: Dict[str, Any] = {
                        "model_image": u1s[0],
                        "garment_image": str(lower_url),
                        "category": "bottoms",
                        "mode": self.fashn_mode,
                        "garment_photo_type": self.fashn_garment_photo_type,
                        "moderation_level": self.fashn_moderation_level,
                        "seed": int(seed),
                        "num_samples": 1,
                        "segmentation_free": bool(self.fashn_segmentation_free),
                        "output_format": self.fashn_output_format,
                    }
                    if self.fashn_sync_mode:
                        fashn_input_2["sync_mode"] = True

                    out2, dbg2 = await self.fal.run_and_wait(endpoint_id=endpoint_id, input_json=fashn_input_2)
                    u2s = _parse_fal_any_image_urls(_as_dict(out2))
                    if not u2s:
                        raise RuntimeError("FASHN_SEQ_STAGE2_NO_URLS")

                    urls.append(u2s[0])
                    debug.append(
                        {
                            "i": i,
                            "seq": True,
                            "seed": int(seed),
                            "stage1": _as_dict(dbg1).get("request_id"),
                            "stage2": _as_dict(dbg2).get("request_id"),
                        }
                    )

            else:
                remaining = n_real
                batch_start = 0
                while remaining > 0:
                    batch_n = min(4, remaining)
                    seed = _stable_seed(req.request_hash, batch_start)

                    fashn_input: Dict[str, Any] = {
                        "model_image": human_url,
                        "garment_image": clothing_primary,
                        "category": cat,
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

                    out, dbg = await self.fal.run_and_wait(endpoint_id=endpoint_id, input_json=fashn_input)
                    out_d = _as_dict(out)
                    batch_urls = _parse_fal_any_image_urls(out_d)

                    if len(batch_urls) < batch_n:
                        raise RuntimeError(
                            f"FASHN returned {len(batch_urls)} images, expected {batch_n}. "
                            f"out_keys={list(out_d.keys())[:30]}"
                        )

                    for j, u in enumerate(batch_urls[:batch_n]):
                        idx = batch_start + j
                        urls.append(u)
                        debug.append(
                            {
                                "i": idx,
                                "url": u,
                                "seed": int(seed),
                                "request_id": _as_dict(dbg).get("request_id"),
                            }
                        )

                    remaining -= batch_n
                    batch_start += batch_n

            return await self._evaluate_non_saree_provider_outputs(
                provider_name="fashn",
                provider_id="fal_fashn",
                route="fashn",
                tag="fashn",
                urls=urls,
                base_meta={
                    "provider": "fashn",
                    "endpoint_id": endpoint_id,
                    "category": cat,
                    "provider_images": n_real,
                    "debug": debug[:8],
                    "sequential": bool(use_sequential),
                },
                garment_type=garment_type,
                human_url=human_url,
                is_outerwear=is_outerwear,
                style_priors=style_priors,
            )

        effective_provider_order = self._resolve_non_saree_provider_order(
            family=indian_non_saree_family or None
        )

        for p in effective_provider_order:
            try:
                if p == "imageapps_v2":
                    provider_candidates.append(await _attempt_imageapps_v2())
                    continue
                if p == "catvton":
                    provider_candidates.append(await _attempt_catvton())
                    continue
                if p == "fashn":
                    provider_candidates.append(await _attempt_fashn())
                    continue
                raise RuntimeError(f"unknown_non_saree_provider {p!r}")
            except Exception as e:
                provider_errors.append({"provider": p, "error": f"{type(e).__name__}: {e}"})
                continue

        resolved_block = {
            "primary_url": primary_url,
            "upper_url": upper_url,
            "lower_url": lower_url,
            "outer_url": outer_url,
            "resolved_platform_garment_kind": resolved_platform_garment_kind,
        }

        if not provider_candidates:
            return self._empty_non_saree_result(
                reason="all_providers_failed",
                garment_type=garment_type,
                indian_non_saree_family=indian_non_saree_family,
                is_outerwear=is_outerwear,
                provider_order=effective_provider_order,
                provider_errors=provider_errors,
                provider_candidates=provider_candidates,
                resolved=resolved_block,
                platform_model_selection=platform_model_selection,
                use_sequential=use_sequential,
                generic_indian_non_saree_fallback_enabled=generic_indian_non_saree_fallback_enabled,
            )

        strict_provider_candidates = [c for c in provider_candidates if len(_as_list(c.get("strict_images"))) > 0]
        hard_provider_candidates = [c for c in provider_candidates if len(_as_list(c.get("hard_images"))) > 0]

        chosen: Optional[Dict[str, Any]] = None
        selection_reason = ""

        if strict_provider_candidates:
            chosen = sorted(strict_provider_candidates, key=lambda c: float(c.get("best_strict_score", -999.0)), reverse=True)[0]
            selection_reason = "best_provider_best_strict_image"
            selected_images = self._pick_final_images_from_provider(chosen=chosen, pool_name="strict")
        elif self.nonsaree_qc.fail_open and hard_provider_candidates:
            chosen = sorted(hard_provider_candidates, key=lambda c: float(c.get("best_hard_score", -999.0)), reverse=True)[0]
            selection_reason = "best_provider_best_hard_image_fail_open"
            selected_images = self._pick_final_images_from_provider(chosen=chosen, pool_name="hard")
        else:
            selected_images = []

        if not chosen or not selected_images:
            return self._empty_non_saree_result(
                reason="no_shippable_outputs_after_qc",
                garment_type=garment_type,
                indian_non_saree_family=indian_non_saree_family,
                is_outerwear=is_outerwear,
                provider_order=effective_provider_order,
                provider_errors=provider_errors,
                provider_candidates=provider_candidates,
                resolved=resolved_block,
                platform_model_selection=platform_model_selection,
                use_sequential=use_sequential,
                generic_indian_non_saree_fallback_enabled=generic_indian_non_saree_fallback_enabled,
            )

        selected_provider = str(chosen["provider"])
        selected_urls = [str(x.get("url")) for x in selected_images if _is_http_url(x.get("url"))]
        route = str(chosen["route"])
        tag = str(chosen["tag"])
        selected_meta = _as_dict(chosen.get("meta"))

        mat_urls, mat_dbg = await self._materialize_variant_urls(
            req=req,
            route=route,
            provider_tag=tag,
            source_urls=selected_urls,
            expand_to_requested_count=False,
        )

        return VTONGenerateResult(
            provider=selected_provider,
            urls=mat_urls,
            meta={
                "route": route,
                "garment_type": garment_type,
                "indian_non_saree_family": indian_non_saree_family,
                "is_outerwear": bool(is_outerwear),
                "provider_order": effective_provider_order,
                "provider_order_configured": self.non_saree_provider_order,
                "generic_indian_non_saree_fallback_enabled": generic_indian_non_saree_fallback_enabled,
                "selection_reason": selection_reason,
                "returned_variants": len(mat_urls),
                "requested_variants": len(req.variants),
                "selected_images": [
                    {
                        "index": x.get("index"),
                        "score": x.get("score"),
                        "ok_strict": x.get("ok_strict"),
                        "ok_hard": x.get("ok_hard"),
                        "reject_reasons_strict": _as_dict(x.get("qc")).get("reject_reasons_strict", []),
                        "reject_reasons_hard": _as_dict(x.get("qc")).get("reject_reasons_hard", []),
                    }
                    for x in selected_images
                ],
                "candidates": [
                    {
                        "provider": c.get("provider"),
                        "qc_summary": _as_dict(c.get("meta")).get("qc_summary"),
                        "image_results": _as_dict(c.get("meta")).get("image_results", []),
                        "route": c.get("route"),
                    }
                    for c in provider_candidates[:8]
                ],
                "errors": provider_errors[:8],
                "resolved": resolved_block,
                "provider_meta": selected_meta,
                "platform_model_selection": platform_model_selection,
                "storage": mat_dbg,
            },
        )