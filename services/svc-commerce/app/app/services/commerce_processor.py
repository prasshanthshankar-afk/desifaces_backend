# services/svc-commerce/app/app/services/commerce_processor.py
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from uuid import UUID

from app.db import get_pool
from app.services.azure_storage_service import AzureStorageConfig, AzureStorageService
from app.services.providers.vton_provider import (
    VTONGenerateRequest,
    VTONProvider,
    VTONVariantSpec,
)

logger = logging.getLogger(__name__)

# -----------------------------
# Generic parsing helpers
# -----------------------------


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (bytes, bytearray)):
        x = x.decode("utf-8", errors="ignore")
    if isinstance(x, str):
        try:
            v = json.loads(x)
            if isinstance(v, str):
                v2 = json.loads(v)
                return v2 if isinstance(v2, dict) else {}
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        v = dict(x)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    if isinstance(x, (bytes, bytearray)):
        x = x.decode("utf-8", errors="ignore")
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


def _merge(d: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(d or {})
    out.update(patch or {})
    return out


def _merge_missing(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fill missing/empty keys in dst from src (dst wins if it has a real value).
    """
    out = dict(dst or {})
    for k, v in (src or {}).items():
        if k not in out or out[k] is None or out[k] == "" or out[k] == {} or out[k] == []:
            out[k] = v
    return out


def _sha256_json(obj: Any) -> str:
    try:
        s = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        s = str(obj)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _stable_seed(*, request_hash: str, idx: int) -> int:
    h = hashlib.sha256(f"{request_hash}:{idx}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


def _coerce_int(x: Any, default: int) -> int:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        try:
            return int(float(str(x)))
        except Exception:
            return default


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


def _extract_quote_id(payload: Dict[str, Any], meta: Dict[str, Any]) -> UUID:
    p = _as_dict(payload)
    m = _as_dict(meta)
    q = (
        _as_dict(p.get("input")).get("quote_id")
        or p.get("quote_id")
        or _as_dict(p.get("quote")).get("quote_id")
        or m.get("quote_id")
    )
    if not q:
        raise RuntimeError("commerce_processor: missing quote_id in payload/meta")
    return UUID(str(q))


def _unwrap_request_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize common wrappers:
      {"request": {...}}
      {"quote_request": {...}}
      {"input": {...}}
    """
    if not d:
        return {}
    if isinstance(d.get("quote_request"), dict):
        return _as_dict(d.get("quote_request"))
    if isinstance(d.get("request"), dict):
        return _as_dict(d.get("request"))
    return d


def _extract_quote_request_anywhere(
    *, payload: Dict[str, Any], meta: Dict[str, Any], campaign_meta: Dict[str, Any]
) -> Dict[str, Any]:
    p = _as_dict(payload)
    m = _as_dict(meta)
    cm = _as_dict(campaign_meta)

    candidates: List[Any] = []
    candidates += [p.get("quote_request"), p.get("request")]
    q = _as_dict(p.get("quote"))
    candidates += [q.get("quote_request"), q.get("request"), q.get("input")]
    candidates += [m.get("quote_request"), m.get("request")]
    candidates += [cm.get("quote_request"), cm.get("request")]

    for c in candidates:
        d = _as_dict(c)
        d = _unwrap_request_dict(d)
        if d:
            return d
    return {}


# -----------------------------
# Variant contract helpers (COMMON pattern for all outfits)
# -----------------------------


def _variant_job_id(*, job_id: UUID, variant_index: int) -> str:
    # canonical: <job_id>-<variant_index>
    return f"{str(job_id)}-{variant_index}"


def _variant_job_ids(*, job_id: UUID, count: int) -> List[str]:
    return [_variant_job_id(job_id=job_id, variant_index=i) for i in range(max(0, count))]


def _normalize_urls(urls: Any) -> List[str]:
    out: List[str] = []
    for u in (urls or []):
        if isinstance(u, str):
            s = u.strip()
            if s:
                out.append(s)
    return out


def _validate_variant_urls_or_raise(
    *,
    job_id: UUID,
    expected_count: int,
    urls: List[str],
    strict: bool,
) -> None:
    """
    Enforce the *common* variant naming pattern across ALL outfits:

      - For expected_count > 1:
          * URLs must not all be identical.
          * Each expected variant tag "<job_id>-<i>" must appear in at least one URL.
    """
    if not urls:
        raise RuntimeError("COMMERCE_NO_OUTPUT_URLS: provider returned empty urls")

    if expected_count <= 1:
        return

    if not strict:
        unique = len(set(urls))
        if unique == 1:
            logger.warning(
                "commerce_processor: variant urls identical (non-strict). job_id=%s expected=%s url=%s",
                job_id, expected_count, urls[0],
            )
        missing = [i for i in range(expected_count) if not any(f"{job_id}-{i}" in u for u in urls)]
        if missing:
            logger.warning(
                "commerce_processor: variant url tags missing (non-strict). job_id=%s expected=%s missing=%s sample=%s",
                job_id, expected_count, missing, urls[0],
            )
        return

    if len(set(urls)) == 1:
        raise RuntimeError(
            f"COMMERCE_VARIANT_URLS_DUPLICATE: expected={expected_count} all_urls_identical url={urls[0]}"
        )

    missing_tags: List[int] = []
    for i in range(expected_count):
        tag = f"{job_id}-{i}"
        if not any(tag in u for u in urls):
            missing_tags.append(i)

    if missing_tags:
        raise RuntimeError(
            "COMMERCE_VARIANT_URLS_MISSING_TAGS: "
            f"job_id={job_id} expected={expected_count} missing={missing_tags} "
            "hint=provider must upload each variant under variant_job_id '<job_id>-<i>'"
        )


# -----------------------------
# Azure helpers
# -----------------------------


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


def _get_storage_service_best_effort() -> Optional[AzureStorageService]:
    """
    Worker-safe AzureStorageService init.
    We only need SAS signing; container is passed explicitly in get_blob_sas_url.
    """
    try:
        return AzureStorageService()
    except Exception as e:
        conn = (
            (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
            or (os.getenv("COMMERCE_AZURE_STORAGE_CONNECTION_STRING") or "").strip()
            or (os.getenv("DF_AZURE_STORAGE_CONNECTION_STRING") or "").strip()
        )
        if not conn:
            logger.warning("commerce_processor: missing Azure connection string for SAS signing err=%r", e)
            return None
        fallback_container = (os.getenv("COMMERCE_OUTPUT_CONTAINER") or "commerce-output").strip() or "commerce-output"
        try:
            cfg = AzureStorageConfig(connection_string=conn, container=fallback_container, default_sas_hours=24)
            return AzureStorageService(config=cfg)
        except Exception as e2:
            logger.warning("commerce_processor: could not init AzureStorageService (fallback) err=%r", e2)
            return None


def _call_storage_get_blob_sas_url_best_effort(
    storage: AzureStorageService,
    *,
    container: str,
    blob_name: str,
    expires_in_s: int,
    permission: str,
) -> str:
    fn = getattr(storage, "get_blob_sas_url", None)
    if not fn or not callable(fn):
        raise RuntimeError("missing_get_blob_sas_url")

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


def _resolve_platform_model_asset_url(
    *,
    storage: Optional[AzureStorageService],
    url: str,
    sas_expires_in_s: int,
) -> str:
    if _is_http_url(url):
        return str(url).strip()
    az = _parse_az_ref(str(url))
    if not az or not storage:
        return str(url)
    c, b = az
    return _call_storage_get_blob_sas_url_best_effort(
        storage,
        container=c,
        blob_name=b,
        expires_in_s=int(sas_expires_in_s),
        permission="r",
    )


# -----------------------------
# Costume gender policy + garment type inference (non-saree)
# -----------------------------


_MALE_ONLY_CODES = {
    "sherwani",
    "kurta_pyjama",
    "kurta_set",
    "dhoti",
    "lungi",
    "pathani",
    "nehru_jacket",
    "bandhgala",
}
_FEMALE_ONLY_CODES = {
    "salwar_suit",
    "salwar_kameez",
    "lehenga",
    "lehenga_set",
    "lehenga_skirt",
    "choli",
    "blouse",
    "dupatta",
    "anarkali",
    "ghagra",
}

_UPPER_CODES = {
    "hoodie",
    "shirt",
    "tshirt",
    "t_shirt",
    "blazer",
    "jacket",
    "coat",
    "kurta",
    "sherwani",
    "top",
    "sweater",
    "cardigan",
}
_LOWER_CODES = {
    "jeans",
    "pants",
    "pant",
    "trousers",
    "trouser",
    "skirt",
    "shorts",
    "pyjama",
    "pajama",
    "dhoti",
    "lungi",
    "lehenga_skirt",
    "ghagra",
}
_DRESS_CODES = {
    "dress",
    "gown",
    "jumpsuit",
    "salwar_suit",
    "salwar_kameez",
    "lehenga",
    "lehenga_set",
    "anarkali",
    "kurta_pyjama",  # treat as set = "dresses" category for provider
    "kurta_set",
    "sherwani",      # treat as set-like for provider too
}


def _infer_target_gender(*, quote_request: Dict[str, Any], product_assets: Dict[str, Any]) -> str:
    # explicit request wins
    qr = _as_dict(quote_request)
    pa = _as_dict(product_assets)

    g = qr.get("gender") or qr.get("target_gender") or _as_dict(qr.get("model_ref")).get("gender")
    if g:
        return _normalize_gender(g)

    # infer from dominant component code / items
    dom = str(pa.get("dominant_component_code") or "").strip().lower()
    if dom in _MALE_ONLY_CODES:
        return "male"
    if dom in _FEMALE_ONLY_CODES:
        return "female"

    for it in _as_list(pa.get("items")):
        d = _as_dict(it)
        code = str(d.get("component_code") or "").strip().lower()
        if code in _MALE_ONLY_CODES:
            return "male"
        if code in _FEMALE_ONLY_CODES:
            return "female"

    return "any"


def _infer_garment_type_from_code(code: str) -> Optional[str]:
    c = (code or "").strip().lower()
    if not c:
        return None
    if c in _DRESS_CODES:
        return "dresses"
    if c in _LOWER_CODES:
        return "lower_body"
    if c in _UPPER_CODES:
        return "upper_body"
    return None


def _normalize_gender(v: Any) -> str:
    s = str(v or "").strip().lower()
    if s in ("m", "male", "man", "boy"):
        return "male"
    if s in ("f", "female", "woman", "girl"):
        return "female"
    return "any"


def _extract_gender_from_model_ref(model_ref: Dict[str, Any]) -> str:
    mr = _as_dict(model_ref)
    g = mr.get("gender") or mr.get("sex")
    if g:
        return _normalize_gender(g)
    meta = _as_dict(mr.get("meta"))
    g2 = meta.get("gender") or meta.get("sex")
    return _normalize_gender(g2)


def _apply_gender_policy_or_raise(
    *,
    target_gender: str,
    model_gender: str,
    dominant_component_code: str,
    strict: bool,
) -> Dict[str, Any]:
    """
    Enforce: sherwani/kurta_pyjama must be male; salwar/lehenga must be female.
    Only enforce if we have a meaningful model_gender (male/female) and target_gender is male/female.
    """
    out: Dict[str, Any] = {
        "target_gender": target_gender,
        "model_gender": model_gender,
        "dominant_component_code": dominant_component_code,
        "strict": strict,
        "ok": True,
        "reason": "",
    }

    tg = _normalize_gender(target_gender)
    mg = _normalize_gender(model_gender)
    if tg == "any" or mg == "any":
        return out

    if tg != mg:
        out["ok"] = False
        out["reason"] = f"GENDER_COSTUME_MISMATCH target_gender={tg} model_gender={mg}"
        if strict:
            raise RuntimeError(out["reason"])
    return out


# -----------------------------
# Platform-model selector helpers
# -----------------------------


def _platform_mode_requested(*, quote_request: Dict[str, Any], product_assets: Dict[str, Any], model_ref: Dict[str, Any]) -> bool:
    qr = _as_dict(quote_request)
    pa_meta = _as_dict(_as_dict(product_assets).get("meta"))
    mr_meta = _as_dict(_as_dict(model_ref).get("meta"))

    mode_blob = " ".join(
        [
            _norm_text(qr.get("mode")),
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

    if _as_dict(model_ref).get("platform_model_id") or mr_meta.get("platform_model_id"):
        return True

    if str(model_ref.get("asset_id") or "").strip() or str(mr_meta.get("asset_id") or "").strip():
        return True

    if str(model_ref.get("human_image_url") or "").strip():
        return False

    return False


def _looks_saree_like_for_platform_selector(*, product_assets: Dict[str, Any]) -> bool:
    pa = _as_dict(product_assets)
    if pa.get("saree_image_url"):
        return True
    blob_parts = [
        _norm_text(pa.get("garment_kind")),
        _norm_text(pa.get("outfit_kind")),
        _norm_text(pa.get("dominant_component_code")),
        _norm_text(pa.get("title")),
        _norm_text(pa.get("name")),
        _norm_text(pa.get("category")),
        _norm_text(pa.get("garment_image_url")),
    ]
    for it in _as_list(pa.get("items")):
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
    blob = " | ".join([x for x in blob_parts if x])
    return any(t in blob for t in ("saree", "sari", "saari", "pallu", "pleat", "kanjivaram", "banarasi"))


def _infer_non_saree_platform_garment_kind(*, product_assets: Dict[str, Any]) -> Optional[str]:
    """
    Resolve to:
      - Indian Phase-1 families when we can
      - else generic families for western / mixed catalog:
          upper_body, lower_body, dresses
    """
    pa = _as_dict(product_assets)
    blob_parts: List[str] = [
        _norm_text(pa.get("garment_kind")),
        _norm_text(pa.get("outfit_kind")),
        _norm_text(pa.get("dominant_component_code")),
        _norm_text(pa.get("title")),
        _norm_text(pa.get("name")),
        _norm_text(pa.get("category")),
        _norm_text(pa.get("garment_image_url")),
        _norm_text(pa.get("primary_image_url")),
        _norm_text(pa.get("product_image_url")),
    ]

    item_codes: List[str] = []
    for it in _as_list(pa.get("items")):
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
        item_codes.extend(
            [
                _norm_text(d.get("component_code") or d.get("kind")),
                _norm_text(d.get("name")),
            ]
        )

    blob = " | ".join([p for p in blob_parts if p])
    joined = " | ".join([x for x in item_codes if x])

    # Indian explicit families first
    if any(t in blob for t in ("dhoti_kurta", "dhoti kurta")):
        return "dhoti_kurta"
    if "sherwani" in blob:
        return "sherwani"
    if any(t in blob for t in ("salwar_suit", "salwar suit", "shalwar", "kameez", "salwar kameez")):
        return "salwar_suit"
    if any(t in blob for t in ("lehenga_set", "lehenga set", "lehenga choli", "lehenga")):
        return "lehenga_set"
    if any(t in blob for t in ("kurta_pyjama", "kurta pyjama", "kurta pajama", "pyjama set", "pajama set")):
        return "kurta_pyjama"

    if "dhoti" in joined and "kurta" in joined:
        return "dhoti_kurta"
    if "lehenga" in joined:
        return "lehenga_set"
    if "salwar" in joined or "kameez" in joined:
        return "salwar_suit"
    if "kurta" in joined and any(t in joined for t in ("pyjama", "pajama")):
        return "kurta_pyjama"
    if "sherwani" in joined:
        return "sherwani"

    # Generic western / mixed fallback
    if any(t in blob for t in ("hoodie", "blazer", "jacket", "coat", "overcoat", "sweater", "cardigan", "shirt", "tshirt", "t-shirt", "top", "kurta", "blouse", "choli")):
        return "upper_body"

    if any(t in blob for t in ("jeans", "pant", "pants", "trouser", "trousers", "skirt", "shorts", "pyjama", "pajama", "dhoti", "lungi")):
        return "lower_body"

    if any(t in blob for t in ("dress", "gown", "jumpsuit", "anarkali", "salwar", "lehenga", "suit", "kurta_set", "onepiece", "one-piece")):
        return "dresses"

    return None


def _resolve_platform_preferred_tags(*, quote_request: Dict[str, Any], product_assets: Dict[str, Any], model_ref: Dict[str, Any]) -> List[str]:
    qr = _as_dict(quote_request)
    pa_meta = _as_dict(_as_dict(product_assets).get("meta"))
    mr_meta = _as_dict(_as_dict(model_ref).get("meta"))
    tags: List[str] = []
    for src in (
        qr.get("style_tags"),
        qr.get("preferred_tags"),
        product_assets.get("style_tags"),
        pa_meta.get("style_tags"),
        model_ref.get("style_tags"),
        mr_meta.get("style_tags"),
        product_assets.get("preferred_tags"),
        pa_meta.get("preferred_tags"),
        model_ref.get("preferred_tags"),
        mr_meta.get("preferred_tags"),
    ):
        tags.extend(_uniq_norm(src))
    return _uniq_norm(tags)


def _resolve_recent_platform_model_codes(*, quote_request: Dict[str, Any], product_assets: Dict[str, Any], model_ref: Dict[str, Any]) -> List[str]:
    qr = _as_dict(quote_request)
    pa_meta = _as_dict(_as_dict(product_assets).get("meta"))
    mr_meta = _as_dict(_as_dict(model_ref).get("meta"))
    codes: List[str] = []
    for src in (
        qr.get("recent_model_codes"),
        product_assets.get("recent_model_codes"),
        pa_meta.get("recent_model_codes"),
        model_ref.get("recent_model_codes"),
        mr_meta.get("recent_model_codes"),
    ):
        codes.extend(_uniq_norm(src))
    return _uniq_norm(codes)


async def _preselect_platform_model_for_non_saree(
    *,
    quote_request: Dict[str, Any],
    product_assets: Dict[str, Any],
    model_ref: Dict[str, Any],
    request_hash: str,
    quote_id: UUID,
    user_id: UUID,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Production-grade platform model selection.

    Uses the approved manifest via services/svc-commerce/app/app/services/catalog/platform_model_selector.py
    and resolves az:// assets to SAS/public URLs for provider consumption.

    Returns:
      (patched_model_ref, debug_meta)
    """
    mr = dict(model_ref or {})
    dbg: Dict[str, Any] = {
        "requested": False,
        "enabled": (os.getenv("COMMERCE_ENABLE_PLATFORM_MODEL_SELECTOR") or "1").strip().lower() not in ("0", "false", "no"),
    }

    if not dbg["enabled"]:
        dbg["reason"] = "selector_disabled"
        return mr, dbg

    if _looks_saree_like_for_platform_selector(product_assets=product_assets):
        dbg["reason"] = "saree_like_skip"
        return mr, dbg

    requested = _platform_mode_requested(
        quote_request=quote_request,
        product_assets=product_assets,
        model_ref=model_ref,
    )
    dbg["requested"] = bool(requested)

    force_when_missing_human = (os.getenv("COMMERCE_PLATFORM_MODEL_FORCE_WHEN_MISSING_HUMAN") or "1").strip().lower() not in ("0", "false", "no")
    human_url_existing = str(mr.get("human_image_url") or mr.get("image_url") or mr.get("url") or "").strip()

    if not requested and not (force_when_missing_human and not human_url_existing):
        dbg["reason"] = "not_requested_and_human_present"
        return mr, dbg

    garment_kind = _infer_non_saree_platform_garment_kind(product_assets=product_assets)
    dbg["resolved_garment_kind"] = garment_kind
    if not garment_kind:
        dbg["reason"] = "garment_kind_unresolved"
        return mr, dbg

    storage = _get_storage_service_best_effort()
    sas_expires_in_s = _coerce_int(os.getenv("COMMERCE_VTON_SAS_EXPIRES_S"), 86400) or 86400

    def _asset_resolver(url: str) -> str:
        return _resolve_platform_model_asset_url(
            storage=storage,
            url=url,
            sas_expires_in_s=sas_expires_in_s,
        )

    from app.services.catalog.platform_model_selector import get_platform_model_selector

    selector = get_platform_model_selector(asset_url_resolver=_asset_resolver)

    pa_meta = _as_dict(_as_dict(product_assets).get("meta"))
    mr_meta = _as_dict(_as_dict(model_ref).get("meta"))

    tenantish = str(
        product_assets.get("tenant_id")
        or pa_meta.get("tenant_id")
        or model_ref.get("tenant_id")
        or mr_meta.get("tenant_id")
        or user_id
    )

    product_id = (
        product_assets.get("product_id")
        or pa_meta.get("product_id")
        or model_ref.get("product_id")
        or mr_meta.get("product_id")
    )

    preferred_tags = _resolve_platform_preferred_tags(
        quote_request=quote_request,
        product_assets=product_assets,
        model_ref=model_ref,
    )
    recent_model_codes = _resolve_recent_platform_model_codes(
        quote_request=quote_request,
        product_assets=product_assets,
        model_ref=model_ref,
    )

    top_k = _coerce_int(os.getenv("COMMERCE_PLATFORM_MODELS_TOP_K"), 10) or 10

    try:
        selection = selector.select_platform_model(
            garment_kind=str(garment_kind),
            tenant_id=str(tenantish),
            quote_id=str(quote_id),
            product_id=str(product_id) if product_id else None,
            preferred_tags=preferred_tags,
            recent_model_codes=recent_model_codes,
            top_k=int(top_k),
        )
    except Exception as e:
        dbg["reason"] = f"selector_failed:{type(e).__name__}:{e}"
        if requested or not human_url_existing:
            raise
        return mr, dbg

    selected_url = str(selection.get("primary_asset_url") or "").strip()
    if not _is_http_url(selected_url):
        dbg["reason"] = "selector_returned_non_http"
        if requested or not human_url_existing:
            raise RuntimeError("platform selector returned non-http primary_asset_url")
        return mr, dbg

    mr["human_image_url"] = selected_url
    if "url" not in mr or not str(mr.get("url") or "").strip():
        mr["url"] = selected_url

    if selection.get("gender") and not mr.get("gender"):
        mr["gender"] = str(selection["gender"])

    meta2 = _as_dict(mr.get("meta"))
    meta2["platform_model_selection"] = selection
    meta2["platform_model_code"] = selection.get("model_code")
    if selection.get("gender") and not meta2.get("gender"):
        meta2["gender"] = selection.get("gender")
    mr["meta"] = meta2

    dbg["selection"] = {
        "model_code": selection.get("model_code"),
        "gender": selection.get("gender"),
        "framing": selection.get("framing"),
        "pose": selection.get("pose"),
        "quality_score": selection.get("quality_score"),
        "primary_asset_url": selection.get("primary_asset_url"),
        "eligible_count": selection.get("eligible_count"),
        "top_k_count": selection.get("top_k_count"),
    }
    dbg["reason"] = "selected"
    dbg["request_hash"] = request_hash[:16]
    return mr, dbg


# -----------------------------
# DB read helpers
# -----------------------------


async def _read_job_payload(con, *, job_id: UUID) -> Dict[str, Any]:
    row = await con.fetchrow(
        """
        select payload_json
        from public.studio_jobs
        where id=$1 and studio_type='commerce'
        """,
        job_id,
    )
    return _as_dict(row["payload_json"] if row else {})


async def _write_job_payload(con, *, job_id: UUID, payload: Dict[str, Any]) -> None:
    await con.execute(
        """
        update public.studio_jobs
        set payload_json=$2::jsonb, updated_at=now()
        where id=$1 and studio_type='commerce'
        """,
        job_id,
        json.dumps(payload or {}, default=str, ensure_ascii=False),
    )


async def _set_job_computed(con, *, job_id: UUID, stage: str, patch: Dict[str, Any] | None = None) -> None:
    payload = await _read_job_payload(con, job_id=job_id)
    computed = _as_dict(payload.get("computed"))
    computed["stage"] = stage
    if patch:
        computed.update(patch)
    payload["computed"] = computed
    payload["stage"] = stage
    await _write_job_payload(con, job_id=job_id, payload=payload)


async def _read_quote_request_from_db(con, *, quote_id: UUID) -> Dict[str, Any]:
    """
    Pull original request_json from public.commerce_quotes and apply resolved_* columns if present.
    """
    try:
        row = await con.fetchrow(
            """
            select to_jsonb(q) as j
            from public.commerce_quotes q
            where q.id = $1
            """,
            quote_id,
        )
    except Exception as e:
        logger.warning("commerce_processor: could not read public.commerce_quotes quote_id=%s err=%s", quote_id, e)
        return {}

    if not row:
        return {}

    j = _as_dict(row.get("j"))

    base: Dict[str, Any] = {}
    for k in ("request_json", "request", "quote_request", "input_json", "payload_json", "meta_json", "quote_json", "input"):
        d = _as_dict(j.get(k))
        d = _unwrap_request_dict(d)
        if d:
            base = d
            break
    base = base or {}

    resolved_garment = j.get("resolved_garment_image_url")
    resolved_human = j.get("resolved_human_image_url")
    dominant_code = j.get("dominant_component_code")
    mode = j.get("mode")
    resolution = j.get("resolution")

    base.setdefault("product_assets", {})
    base.setdefault("model_ref", {})

    pa = _as_dict(base.get("product_assets"))
    mr = _as_dict(base.get("model_ref"))

    if isinstance(resolved_garment, str) and resolved_garment.strip():
        pa.setdefault("garment_image_url", resolved_garment.strip())
    if isinstance(dominant_code, str) and dominant_code.strip():
        pa.setdefault("dominant_component_code", dominant_code.strip())
    if isinstance(resolved_human, str) and resolved_human.strip():
        mr.setdefault("human_image_url", resolved_human.strip())

    if isinstance(mode, str) and mode.strip():
        base.setdefault("mode", mode.strip())
    if isinstance(resolution, str) and resolution.strip():
        base.setdefault("resolution", resolution.strip())

    base["product_assets"] = pa
    base["model_ref"] = mr
    return base


async def _persist_quote_resolved_best_effort(
    con,
    *,
    quote_id: UUID,
    mode: str,
    resolution: str,
    dominant_component_code: Optional[str],
    garment_url: Optional[str],
    human_url: Optional[str],
    resolved_json: Dict[str, Any],
) -> None:
    try:
        await con.execute(
            """
            update public.commerce_quotes
            set
              resolved_json = $2::jsonb,
              mode = $3,
              resolution = $4,
              dominant_component_code = $5,
              resolved_garment_image_url = $6,
              resolved_human_image_url = $7,
              updated_at = now()
            where id = $1
            """,
            quote_id,
            json.dumps(resolved_json or {}, default=str, ensure_ascii=False),
            mode,
            resolution,
            dominant_component_code,
            garment_url,
            human_url,
        )
    except Exception as e:
        logger.warning("commerce_processor: persist resolved quote fields failed quote_id=%s err=%s", quote_id, e)


# -----------------------------
# VTON input normalizers
# -----------------------------


def _minify_provider_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(meta or {})
    m.pop("raw", None)
    dbg = m.get("debug")
    if isinstance(dbg, list):
        slim: List[Dict[str, Any]] = []
        for item in dbg[:5]:
            if isinstance(item, dict):
                slim.append({"i": item.get("i"), "url": item.get("url")})
        m["debug"] = slim
    return m


def _pick_best_image_from_item(item: Dict[str, Any]) -> Optional[str]:
    u = item.get("image_url") or item.get("url")
    if isinstance(u, str) and u.strip():
        return u.strip()
    alts = _as_list(item.get("image_urls"))
    for a in alts:
        if isinstance(a, str) and a.strip():
            return a.strip()
    return None


def _score_item(item: Dict[str, Any], catalog_rank: Optional[int]) -> int:
    score = 0
    if bool(item.get("is_primary")):
        score += 10_000

    kind = str(item.get("kind") or "garment").strip().lower()
    if kind == "garment":
        score += 1_000
    elif kind in ("accessory", "jewelry"):
        score -= 250

    rank = item.get("dominance_rank")
    if rank is None:
        rank = catalog_rank
    r = _coerce_int(rank, default=9999)
    score += max(0, 500 - r)
    return score


async def _fetch_catalog_ranks_best_effort(con, component_codes: List[str]) -> Dict[str, int]:
    codes = [c for c in component_codes if isinstance(c, str) and c.strip()]
    if not codes:
        return {}
    try:
        rows = await con.fetch(
            """
            select code, dominance_rank
            from public.commerce_garment_components
            where code = any($1::text[])
            """,
            codes,
        )
        out: Dict[str, int] = {}
        for r in rows or []:
            code = str(r["code"])
            out[code] = _coerce_int(r["dominance_rank"], default=9999)
        return out
    except Exception:
        return {}


async def _apply_items_resolver_best_effort(con, *, product_assets: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    pa = dict(product_assets or {})
    items = _as_list(pa.get("items"))
    if not items:
        return pa, None

    norm_items: List[Dict[str, Any]] = []
    codes: List[str] = []
    for it in items:
        d = _as_dict(it)
        if not d:
            continue
        code = str(d.get("component_code") or "").strip()
        if code:
            codes.append(code)
        norm_items.append(d)

    catalog = await _fetch_catalog_ranks_best_effort(con, codes)

    best: Optional[Dict[str, Any]] = None
    best_score = -10**9
    best_code: Optional[str] = None

    for it in norm_items:
        code = str(it.get("component_code") or "").strip()
        score = _score_item(it, catalog.get(code))
        if score > best_score:
            best_score = score
            best = it
            best_code = code or None

    if best:
        picked_url = _pick_best_image_from_item(best)
        if picked_url:
            pa["garment_image_url"] = picked_url
        if best_code:
            pa["dominant_component_code"] = best_code

    return pa, best_code


def _ensure_human_image_url(model_ref: Dict[str, Any]) -> Dict[str, Any]:
    mr = dict(model_ref or {})
    if isinstance(mr.get("human_image_url"), str) and mr["human_image_url"].strip():
        return mr
    for k in ("image_url", "url", "ref_url", "photo_url"):
        v = mr.get(k)
        if isinstance(v, str) and v.strip():
            mr["human_image_url"] = v.strip()
            return mr
    return mr


def _ensure_garment_image_url(product_assets: Dict[str, Any]) -> Dict[str, Any]:
    pa = dict(product_assets or {})
    if isinstance(pa.get("garment_image_url"), str) and pa["garment_image_url"].strip():
        return pa
    for k in ("product_image_url", "primary_image_url", "saree_image_url", "blouse_image_url"):
        v = pa.get(k)
        if isinstance(v, str) and v.strip():
            pa["garment_image_url"] = v.strip()
            return pa
    return pa


def _apply_full_body_hints(
    *, quote_request: Dict[str, Any], product_assets: Dict[str, Any], model_ref: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """
    IMPORTANT for COMMERCE_SAREE_STRICT=1:
    vton_provider's saree_drape pipeline needs a "full body" signal.

    For platform_models (vendor) we FORCE full_body=True.
    """
    qr = _as_dict(quote_request)
    mode = str(qr.get("mode") or "platform_models").strip() or "platform_models"

    views = _as_dict(qr.get("views"))
    full_body = bool(views.get("full_body", True))

    if mode == "platform_models":
        full_body = True

    pa = dict(product_assets or {})
    mr = dict(model_ref or {})

    pa_meta = _as_dict(pa.get("meta"))
    pa_views = _as_dict(pa_meta.get("views"))
    pa_views["full_body"] = full_body
    pa_meta["views"] = pa_views
    pa["meta"] = pa_meta

    mr_meta = _as_dict(mr.get("meta"))
    mr_views = _as_dict(mr_meta.get("views"))
    mr_views["full_body"] = full_body
    mr_meta["views"] = mr_views
    mr_meta["full_body"] = full_body
    mr_meta["is_full_body"] = full_body
    mr["meta"] = mr_meta
    mr["full_body"] = full_body
    mr["is_full_body"] = full_body

    return pa, mr, full_body


def _extract_vton_request_parts(
    *, quote_request: Dict[str, Any], payload: Dict[str, Any], quote_id: UUID
) -> Tuple[Dict[str, Any], Dict[str, Any], str, str, int, str, Dict[str, Any]]:
    p = _as_dict(payload)
    inp = _as_dict(p.get("input"))
    qr = _unwrap_request_dict(_as_dict(quote_request))

    outputs = _as_dict(qr.get("outputs"))
    count = _coerce_int(outputs.get("num_images"), 0)
    if count <= 0:
        count = _coerce_int(qr.get("count"), 4)
    count = max(1, min(count, 24))

    language = str(qr.get("language") or p.get("language") or inp.get("language") or "en").strip() or "en"

    resolution = str(qr.get("resolution") or p.get("resolution") or inp.get("resolution") or "hd").strip() or "hd"
    if resolution not in ("sd", "hd", "hi_res"):
        resolution = "hd"

    product_assets = _as_dict(qr.get("product_assets") or p.get("product_assets") or inp.get("product_assets"))
    model_ref = _as_dict(qr.get("model_ref") or p.get("model_ref") or inp.get("model_ref"))

    # normalize legacy keys into dicts
    for k in (
        "garment_image_url",
        "saree_image_url",
        "blouse_image_url",
        "primary_image_url",
        "product_image_url",
        "product_type",
        "cloth_type",
        "items",
        "meta",
        "dominant_component_code",
        "garment_type",
        "outfit_kind",
        "garment_kind",
        "mode",
        "preferred_tags",
        "style_tags",
        "recent_model_codes",
    ):
        if k in qr and k not in product_assets:
            product_assets[k] = qr.get(k)
        if k in inp and k not in product_assets:
            product_assets[k] = inp.get(k)

    for k in (
        "human_image_url",
        "image_url",
        "url",
        "ref_url",
        "photo_url",
        "platform_model_id",
        "asset_id",
        "meta",
        "gender",
        "sex",
        "use_platform_models",
        "platform_model_required",
        "mode",
        "preferred_tags",
        "style_tags",
        "recent_model_codes",
    ):
        if k in qr and k not in model_ref:
            model_ref[k] = qr.get(k)
        if k in inp and k not in model_ref:
            model_ref[k] = inp.get(k)

    model_ref = _ensure_human_image_url(model_ref)
    product_assets = _ensure_garment_image_url(product_assets)

    request_hash = _sha256_json(
        {
            "quote_id": str(quote_id),
            "count": count,
            "language": language,
            "resolution": resolution,
            "product_assets": product_assets,
            "model_ref": model_ref,
        }
    )

    debug_inputs = {
        "count": count,
        "language": language,
        "resolution": resolution,
        "human_image_url": model_ref.get("human_image_url") or model_ref.get("url") or model_ref.get("image_url"),
        "garment_image_url": product_assets.get("garment_image_url")
        or product_assets.get("product_image_url")
        or product_assets.get("primary_image_url"),
        "has_items": bool(_as_list(product_assets.get("items"))),
        "dominant_component_code": product_assets.get("dominant_component_code"),
        "mode": str(qr.get("mode") or "platform_models"),
        "garment_type": product_assets.get("garment_type"),
        "outfit_kind": product_assets.get("outfit_kind"),
        "garment_kind": product_assets.get("garment_kind"),
    }

    return product_assets, model_ref, language, resolution, count, request_hash, debug_inputs


def _build_variants(*, quote_request: Dict[str, Any], request_hash: str, count: int) -> List[VTONVariantSpec]:
    qr = _as_dict(quote_request)
    drapes = qr.get("drape_styles") if isinstance(qr.get("drape_styles"), list) else []
    drape_style = str(drapes[0]) if drapes else (str(qr.get("drape_style")) if qr.get("drape_style") else None)

    poses = ["standing_full", "three_quarter", "walking", "sitting", "drape_closeup", "pallu_closeup", "border_macro"]
    bgs = ["studio_white", "festive", "outdoor", "indoor_soft"]

    variants: List[VTONVariantSpec] = []
    for i in range(count):
        pose = poses[i % len(poses)]
        bg = bgs[(i // len(poses)) % len(bgs)]
        seed = _stable_seed(request_hash=request_hash, idx=i)
        variants.append(VTONVariantSpec(pose=pose, background=bg, drape_style=drape_style, seed=seed))
    return variants


# -----------------------------
# Non-saree QC (best-of-N selection)
# -----------------------------


class NonSareeQC:
    """
    Fast, cheap QC that compares output vs human in ROIs.

    Goals:
      - Ensure the target garment region changed enough (upper/lower/both)
      - Ensure the non-target region did NOT get destroyed (outfit completeness)
      - Preserve face region (avoid identity destruction)
    """

    def __init__(self) -> None:
        self.enabled = (os.getenv("COMMERCE_VTON_QC_ENABLE") or "1").strip().lower() not in ("0", "false", "no")
        self.strict = (os.getenv("COMMERCE_VTON_QC_STRICT") or "0").strip().lower() in ("1", "true", "yes", "y", "on")
        self.timeout_s = _coerce_int(os.getenv("COMMERCE_VTON_QC_TIMEOUT_S"), 25) or 25
        self.image_size = _coerce_int(os.getenv("COMMERCE_VTON_QC_IMAGE_SIZE"), 256) or 256

        # minimum diffs (presence)
        self.min_upper = float(os.getenv("COMMERCE_VTON_QC_MIN_UPPER_DIFF") or "0.04")
        self.min_lower = float(os.getenv("COMMERCE_VTON_QC_MIN_LOWER_DIFF") or "0.04")
        self.min_both_upper = float(os.getenv("COMMERCE_VTON_QC_MIN_DRESS_UPPER_DIFF") or "0.03")
        self.min_both_lower = float(os.getenv("COMMERCE_VTON_QC_MIN_DRESS_LOWER_DIFF") or "0.05")

        # maximum diffs (preserve)
        self.max_face = float(os.getenv("COMMERCE_VTON_QC_MAX_FACE_DIFF") or "0.14")
        self.max_corners = float(os.getenv("COMMERCE_VTON_QC_MAX_CORNER_DIFF") or "0.15")

        # outfit completeness: non-target should not be wildly altered
        self.max_non_target = float(os.getenv("COMMERCE_VTON_QC_MAX_NON_TARGET_DIFF") or "0.20")

    async def score(
        self,
        *,
        human_url: str,
        out_url: str,
        garment_type: str,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"qc_enabled": False, "ok": True, "score": 0.0}

        from PIL import Image, ImageChops, ImageStat
        import io

        size = int(self.image_size)
        timeout_s = int(self.timeout_s)

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

        human = await asyncio.to_thread(_fetch, human_url)
        out = await asyncio.to_thread(_fetch, out_url)

        # Regions
        upper = _roi_diff(human, out, 0.10, 0.90, 0.18, 0.60)  # torso-ish
        lower = _roi_diff(human, out, 0.08, 0.92, 0.60, 0.98)  # legs/skirt area

        face = _roi_diff(human, out, 0.30, 0.70, 0.00, 0.28)

        c1 = _roi_diff(human, out, 0.00, 0.18, 0.00, 0.18)
        c2 = _roi_diff(human, out, 0.82, 1.00, 0.00, 0.18)
        c3 = _roi_diff(human, out, 0.00, 0.18, 0.82, 1.00)
        c4 = _roi_diff(human, out, 0.82, 1.00, 0.82, 1.00)
        corners = (c1 + c2 + c3 + c4) / 4.0

        gt = (garment_type or "upper_body").strip().lower()
        ok_presence = True
        ok_preserve = True

        # Presence constraints
        if gt == "upper_body":
            ok_presence = upper >= self.min_upper
        elif gt == "lower_body":
            ok_presence = lower >= self.min_lower
        else:  # dresses / one-piece / sets
            ok_presence = (upper >= self.min_both_upper) and (lower >= self.min_both_lower)

        # Preserve constraints
        if face > self.max_face or corners > self.max_corners:
            ok_preserve = False

        # Outfit completeness:
        # - For upper_body, lower should not be massively changed
        # - For lower_body, upper should not be massively changed
        if gt == "upper_body" and lower > self.max_non_target:
            ok_preserve = False
        if gt == "lower_body" and upper > self.max_non_target:
            ok_preserve = False

        ok = bool(ok_presence and ok_preserve)

        # Score: favor strong presence, penalize face/corners
        score = float(upper + lower) - float(2.0 * face) - float(1.0 * corners)

        return {
            "qc_enabled": True,
            "ok": ok,
            "score": score,
            "garment_type": gt,
            "upper": upper,
            "lower": lower,
            "face": face,
            "corners": corners,
            "thresholds": {
                "min_upper": self.min_upper,
                "min_lower": self.min_lower,
                "min_dress_upper": self.min_both_upper,
                "min_dress_lower": self.min_both_lower,
                "max_face": self.max_face,
                "max_corners": self.max_corners,
                "max_non_target": self.max_non_target,
            },
        }


# -----------------------------
# Main worker entry
# -----------------------------


async def process_commerce_job(*, job_id: UUID, payload: Dict[str, Any], meta: Dict[str, Any], user_id: UUID) -> None:
    payload = _as_dict(payload)
    meta = _as_dict(meta)

    quote_id = _extract_quote_id(payload, meta)
    started_at = datetime.now(timezone.utc).isoformat()
    pool = await get_pool()

    campaign_id: Optional[UUID] = None
    merged_meta: Dict[str, Any] = {}
    campaign_meta: Dict[str, Any] = {}

    async with pool.acquire() as con:
        await _set_job_computed(con, job_id=job_id, stage="running", patch={"started_at": started_at, "processor": "vton_v3"})

        camp = await con.fetchrow(
            """
            select id, status, meta_json
            from public.commerce_campaigns
            where user_id=$1 and (meta_json->>'studio_job_id')=$2
            order by created_at desc
            limit 1
            """,
            user_id,
            str(job_id),
        )
        if not camp:
            camp = await con.fetchrow(
                """
                select id, status, meta_json
                from public.commerce_campaigns
                where user_id=$1 and quote_id=$2
                order by created_at desc
                limit 1
                """,
                user_id,
                quote_id,
            )
        if not camp:
            raise RuntimeError(f"commerce_processor: commerce_campaign not found for quote_id={quote_id}")

        campaign_id = UUID(str(camp["id"]))
        campaign_meta = _as_dict(camp["meta_json"])

        merged_meta = _merge(
            campaign_meta,
            {
                "studio_job_id": str(job_id),
                "quote_id": str(quote_id),
                "commerce_campaign_id": str(campaign_id),
                "processor": "vton_v3",
                "started_at": started_at,
            },
        )
        await con.execute(
            """
            update public.commerce_campaigns
            set status='running', meta_json=$2::jsonb, updated_at=now()
            where id=$1
            """,
            campaign_id,
            json.dumps(merged_meta, default=str, ensure_ascii=False),
        )

    assert campaign_id is not None

    try:
        quote_request = _extract_quote_request_anywhere(payload=payload, meta=meta, campaign_meta=campaign_meta)

        async with pool.acquire() as con:
            db_req = await _read_quote_request_from_db(con, quote_id=quote_id)

        quote_request = _merge_missing(quote_request, db_req)

        product_assets, model_ref, language, resolution, count, request_hash, debug_inputs = _extract_vton_request_parts(
            quote_request=quote_request, payload=payload, quote_id=quote_id
        )

        # Resolve dominant garment from items[] (best-effort)
        async with pool.acquire() as con:
            product_assets, dominant_code = await _apply_items_resolver_best_effort(con, product_assets=product_assets)

        # Merge DB-resolved nested dicts even if payload had partial dicts
        product_assets = _merge_missing(product_assets, _as_dict(db_req.get("product_assets")))
        model_ref = _merge_missing(model_ref, _as_dict(db_req.get("model_ref")))

        product_assets = _ensure_garment_image_url(product_assets)
        model_ref = _ensure_human_image_url(model_ref)

        # Determine dominant component code
        dominant_component_code = str(product_assets.get("dominant_component_code") or dominant_code or "").strip()

        # Infer garment_type from code if missing (helps provider category)
        if not str(product_assets.get("garment_type") or "").strip() and dominant_component_code:
            gt = _infer_garment_type_from_code(dominant_component_code)
            if gt:
                product_assets["garment_type"] = gt

        # Determine target gender from costume rules
        target_gender = _infer_target_gender(quote_request=quote_request, product_assets=product_assets)

        # Apply platform-model preselection (production-grade approved manifest)
        platform_pick_dbg: Dict[str, Any] = {}
        try:
            model_ref, platform_pick_dbg = await _preselect_platform_model_for_non_saree(
                quote_request=quote_request,
                product_assets=product_assets,
                model_ref=model_ref,
                request_hash=request_hash,
                quote_id=quote_id,
                user_id=user_id,
            )
        except Exception as e:
            raise RuntimeError(f"commerce_processor: platform model preselection failed err={type(e).__name__}: {e}") from e

        model_ref = _ensure_human_image_url(model_ref)

        # Inject full-body hints so Saree strict pipeline doesn't fail with SAREE_DRAPE_REQUIRES_FULL_BODY
        product_assets, model_ref, full_body = _apply_full_body_hints(
            quote_request=quote_request,
            product_assets=product_assets,
            model_ref=model_ref,
        )

        garment_url = product_assets.get("garment_image_url")
        human_url = model_ref.get("human_image_url")

        provider = VTONProvider()

        must_have_inputs = bool(provider.enable_real and provider.provider == "fal" and not getattr(provider, "demo_mode", False))

        # Allow human_url to be missing only if provider selector is expected to fill it later.
        provider_selector_enabled = (os.getenv("COMMERCE_ENABLE_PLATFORM_MODEL_SELECTOR") or "1").strip().lower() not in ("0", "false", "no")
        provider_force_when_missing = (os.getenv("COMMERCE_PLATFORM_MODEL_FORCE_WHEN_MISSING_HUMAN") or "1").strip().lower() not in ("0", "false", "no")
        platform_requested = _platform_mode_requested(
            quote_request=quote_request,
            product_assets=product_assets,
            model_ref=model_ref,
        )
        allow_missing_human_for_provider_selector = bool(provider_selector_enabled and (platform_requested or provider_force_when_missing))

        if must_have_inputs:
            if not (isinstance(garment_url, str) and garment_url.strip()):
                raise RuntimeError("commerce_processor: missing garment_image_url (provide product_assets.items[] or garment_image_url)")
            if not (isinstance(human_url, str) and human_url.strip()) and not allow_missing_human_for_provider_selector:
                raise RuntimeError("commerce_processor: missing human_image_url (provide model_ref.image_url or model_ref.human_image_url)")
        else:
            if not (isinstance(garment_url, str) and garment_url.strip()):
                logger.warning("commerce_processor: garment_image_url missing; proceeding (demo/placeholder). quote_id=%s", quote_id)
                garment_url = None
            if not (isinstance(human_url, str) and human_url.strip()) and not allow_missing_human_for_provider_selector:
                logger.warning("commerce_processor: human_image_url missing; proceeding (demo/placeholder). quote_id=%s", quote_id)
                human_url = None

        # Gender-costume enforcement (optional strict)
        strict_gender = (os.getenv("COMMERCE_GENDER_COSTUME_STRICT") or "0").strip().lower() in ("1", "true", "yes", "y", "on")
        model_gender = _extract_gender_from_model_ref(model_ref)
        gender_policy_dbg = _apply_gender_policy_or_raise(
            target_gender=target_gender,
            model_gender=model_gender,
            dominant_component_code=dominant_component_code,
            strict=strict_gender,
        )

        # Persist resolved info (quote table)
        resolved_json = {
            "source": "commerce_processor",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "mode": str(_as_dict(quote_request).get("mode") or "platform_models"),
            "resolution": resolution,
            "full_body": full_body,
            "dominant_component_code": dominant_component_code or None,
            "resolved_garment_image_url": garment_url,
            "resolved_human_image_url": human_url,
            "target_gender": target_gender,
            "model_gender": model_gender,
            "gender_policy": gender_policy_dbg,
            "platform_model_preselection": platform_pick_dbg,
            "product_assets": product_assets,
            "model_ref": model_ref,
        }

        async with pool.acquire() as con:
            await _persist_quote_resolved_best_effort(
                con,
                quote_id=quote_id,
                mode=str(_as_dict(quote_request).get("mode") or "platform_models"),
                resolution=resolution,
                dominant_component_code=dominant_component_code or None,
                garment_url=garment_url,
                human_url=human_url,
                resolved_json=resolved_json,
            )

        variants = _build_variants(quote_request=quote_request, request_hash=request_hash, count=count)
        expected_variant_count = len(variants)
        expected_variant_job_ids = _variant_job_ids(job_id=job_id, count=expected_variant_count)

        debug_inputs = dict(debug_inputs or {})
        debug_inputs.update(
            {
                "garment_image_url": garment_url,
                "human_image_url": human_url,
                "dominant_component_code": dominant_component_code,
                "full_body": full_body,
                "provider_enable_real": provider.enable_real,
                "provider_name": provider.provider,
                "provider_demo_mode": getattr(provider, "demo_mode", False),
                "target_gender": target_gender,
                "model_gender": model_gender,
                "gender_policy": gender_policy_dbg,
                "platform_model_preselection": platform_pick_dbg,
                "expected_variant_count": expected_variant_count,
                "expected_variant_job_ids": expected_variant_job_ids[:10],
            }
        )

        async with pool.acquire() as con:
            await _set_job_computed(
                con,
                job_id=job_id,
                stage="running",
                patch={"request_hash": request_hash, "debug_inputs": debug_inputs},
            )

        req = VTONGenerateRequest(
            user_id=user_id,
            studio_job_id=job_id,
            commerce_campaign_id=campaign_id,
            quote_id=quote_id,
            request_hash=request_hash,
            product_assets=product_assets,
            model_ref=model_ref,
            language=language,
            resolution=resolution,
            variants=variants,
        )

        # Call provider
        result = await provider.generate(req)

        # Extract urls
        urls = None
        if isinstance(result, dict):
            urls = result.get("urls") or _as_dict(result.get("computed")).get("urls")
        else:
            urls = getattr(result, "urls", None)

        urls = _normalize_urls(urls)
        bad = [u for u in urls if not u.startswith("http")]
        if bad:
            raise RuntimeError(f"COMMERCE_BAD_OUTPUT_URLS: {bad[:3]}")

        provider_name = None
        provider_meta: Dict[str, Any] = {}
        if isinstance(result, dict):
            provider_name = str(result.get("provider") or "unknown")
            provider_meta = _as_dict(result.get("meta"))
        else:
            provider_name = str(getattr(result, "provider", "") or "unknown")
            provider_meta = _as_dict(getattr(result, "meta", None))

        # If provider selected platform model internally, use that human url for downstream QC/persistence.
        provider_platform_sel = _as_dict(provider_meta.get("platform_model_selection"))
        if provider_platform_sel:
            purl = str(provider_platform_sel.get("primary_asset_url") or "").strip()
            if _is_http_url(purl):
                human_url = purl
                model_ref["human_image_url"] = purl
                meta2 = _as_dict(model_ref.get("meta"))
                meta2["platform_model_selection"] = provider_platform_sel
                model_ref["meta"] = meta2
                if provider_platform_sel.get("gender") and not model_ref.get("gender"):
                    model_ref["gender"] = str(provider_platform_sel.get("gender"))

        # Enforce common variant tag contract (STRICT by default)
        strict_variants = (os.getenv("COMMERCE_STRICT_VARIANT_TAGS") or "1").strip().lower() not in ("0", "false", "no")
        _validate_variant_urls_or_raise(
            job_id=job_id,
            expected_count=expected_variant_count,
            urls=urls,
            strict=strict_variants,
        )

        # -----------------------------
        # Non-saree QC + best selection
        # (Saree pipeline remains unchanged inside provider)
        # -----------------------------
        qc = NonSareeQC()
        qc_summary: Dict[str, Any] = {"qc_enabled": qc.enabled, "qc_strict": qc.strict}
        best_idx: Optional[int] = None
        ranked: List[Dict[str, Any]] = []
        if qc.enabled and isinstance(human_url, str) and human_url.startswith("http") and urls:
            gt = (
                str(product_assets.get("garment_type") or "").strip().lower()
                or _infer_garment_type_from_code(dominant_component_code)
                or "upper_body"
            )
            for i, u in enumerate(urls[:expected_variant_count]):
                try:
                    r = await qc.score(human_url=str(human_url), out_url=str(u), garment_type=gt)
                except Exception as e:
                    r = {"qc_enabled": True, "ok": False, "score": -999.0, "error": f"{type(e).__name__}: {e}"}
                ranked.append({"i": i, "url": u, **_as_dict(r)})

            # pick best ok, else best score
            ok_items = [x for x in ranked if x.get("ok") is True]
            if ok_items:
                ok_items_sorted = sorted(ok_items, key=lambda x: float(x.get("score") or -999.0), reverse=True)
                best_idx = int(ok_items_sorted[0]["i"])
            else:
                ranked_sorted = sorted(ranked, key=lambda x: float(x.get("score") or -999.0), reverse=True)
                best_idx = int(ranked_sorted[0]["i"]) if ranked_sorted else None

            qc_summary.update(
                {
                    "garment_type": gt,
                    "ranked": ranked[: min(12, len(ranked))],
                    "best_variant_index": best_idx,
                    "best_url": (urls[best_idx] if best_idx is not None and best_idx < len(urls) else None),
                    "ok_count": len([x for x in ranked if x.get("ok") is True]),
                }
            )

            if qc.strict and not any(x.get("ok") is True for x in ranked):
                raise RuntimeError(f"COMMERCE_VTON_QC_STRICT_FAILED qc={qc_summary}")

        finished_at = datetime.now(timezone.utc).isoformat()

        async with pool.acquire() as con:
            await _set_job_computed(
                con,
                job_id=job_id,
                stage="succeeded",
                patch={
                    "finished_at": finished_at,
                    "expected_variant_count": expected_variant_count,
                    "variant_count": len(urls),
                    "expected_variant_job_ids": expected_variant_job_ids,
                    "urls": urls,
                    "best_variant_index": best_idx,
                    "best_url": (urls[best_idx] if best_idx is not None and best_idx < len(urls) else None),
                    "qc": qc_summary,
                    "provider": provider_name,
                    "provider_meta": _minify_provider_meta(provider_meta),
                    "platform_model_selection": provider_platform_sel or _as_dict(platform_pick_dbg.get("selection")),
                    "commerce_campaign_id": str(campaign_id),
                    "quote_id": str(quote_id),
                    "request_hash": request_hash,
                },
            )

            merged_meta2 = _merge(
                merged_meta,
                {
                    "finished_at": finished_at,
                    "status": "succeeded",
                    "provider": provider_name,
                    "request_hash": request_hash,
                    "best_variant_index": best_idx,
                    "platform_model_selection": provider_platform_sel or _as_dict(platform_pick_dbg.get("selection")),
                },
            )
            await con.execute(
                """
                update public.commerce_campaigns
                set status='succeeded', meta_json=$2::jsonb, updated_at=now()
                where id=$1
                """,
                campaign_id,
                json.dumps(merged_meta2, default=str, ensure_ascii=False),
            )

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.exception("commerce_processor: job failed job_id=%s quote_id=%s", job_id, quote_id)
        failed_at = datetime.now(timezone.utc).isoformat()

        async with pool.acquire() as con:
            await _set_job_computed(
                con,
                job_id=job_id,
                stage="failed",
                patch={
                    "failed_at": failed_at,
                    "error": err[:2000],
                    "commerce_campaign_id": str(campaign_id) if campaign_id else None,
                    "quote_id": str(quote_id),
                },
            )
            try:
                merged_meta_fail = _merge(merged_meta, {"failed_at": failed_at, "status": "failed", "error": err[:2000]})
                await con.execute(
                    """
                    update public.commerce_campaigns
                    set status='failed', meta_json=$2::jsonb, updated_at=now()
                    where id=$1
                    """,
                    campaign_id,
                    json.dumps(merged_meta_fail, default=str, ensure_ascii=False),
                )
            except Exception:
                pass
        raise