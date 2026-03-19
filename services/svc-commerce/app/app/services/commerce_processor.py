# services/svc-commerce/app/app/services/commerce_processor.py
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from azure.storage.blob import BlobServiceClient

from app.db import get_pool
from app.services.azure_storage_service import AzureStorageConfig, AzureStorageService
from app.services.providers.vton_provider import (
    VTONGenerateRequest,
    VTONProvider,
    VTONVariantSpec,
)

PRICING_IMPORT_ERROR: Optional[str] = None

try:
    from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
    from desifaces_shared.pricing.models import PricingCommitRequest, PricingReleaseRequest
except Exception as pricing_import_error:  # pragma: no cover
    PRICING_IMPORT_ERROR = str(pricing_import_error)

    class PricingClientError(Exception):
        pass

    @dataclass
    class PricingCommitRequest:
        user_id: str
        reservation_id: str
        actual_units: str
        external_ref_type: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]

    @dataclass
    class PricingReleaseRequest:
        user_id: str
        reservation_id: str
        reason: str
        external_ref_type: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]

    class SvcPricingClient:
        enabled = False

        @classmethod
        def from_env(cls, service_name: str) -> "SvcPricingClient":
            return cls()

        async def commit(self, req: PricingCommitRequest):
            raise PricingClientError("pricing client unavailable")

        async def release(self, req: PricingReleaseRequest):
            raise PricingClientError("pricing client unavailable")


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


def _stable_pick_index(*, seed_material: str, n: int) -> int:
    if n <= 0:
        raise RuntimeError("commerce_processor: no platform-model candidates available")
    h = hashlib.sha256(seed_material.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % n


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
    Enforce the common variant naming pattern across all outfits.

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
                job_id,
                expected_count,
                urls[0],
            )
        missing = [i for i in range(expected_count) if not any(f"{job_id}-{i}" in u for u in urls)]
        if missing:
            logger.warning(
                "commerce_processor: variant url tags missing (non-strict). job_id=%s expected=%s missing=%s sample=%s",
                job_id,
                expected_count,
                missing,
                urls[0],
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


def _platform_model_container_name() -> str:
    return (os.getenv("COMMERCE_PLATFORM_MODEL_CONTAINER") or "commerce-catalog").strip() or "commerce-catalog"


def _platform_model_prefix() -> str:
    return (os.getenv("COMMERCE_PLATFORM_MODEL_PREFIX") or "platform_models").strip().strip("/") or "platform_models"


def _platform_model_source_file() -> str:
    return (os.getenv("COMMERCE_PLATFORM_MODEL_SOURCE_FILE") or "source.jpg").strip() or "source.jpg"


def _platform_model_max_candidates() -> int:
    return max(1, _coerce_int(os.getenv("COMMERCE_PLATFORM_MODEL_MAX_CANDIDATES"), 500) or 500)


def _is_desifaces_platform_model_az_ref(v: Any) -> bool:
    az = _parse_az_ref(str(v or ""))
    if not az:
        return False
    c, b = az
    prefix = _platform_model_prefix() + "/"
    return c == _platform_model_container_name() and b.startswith(prefix)


def _is_desifaces_platform_model_http_url(v: Any) -> bool:
    s = str(v or "").strip()
    if not _is_http_url(s):
        return False
    parsed = urlparse(s)
    path = parsed.path.lstrip("/")
    expected_prefix = f"{_platform_model_container_name()}/{_platform_model_prefix()}/"
    return path.startswith(expected_prefix)


def _is_desifaces_platform_model_ref(v: Any) -> bool:
    return _is_desifaces_platform_model_az_ref(v) or _is_desifaces_platform_model_http_url(v)


def _assert_desifaces_platform_model_ref(*, value: Any, label: str) -> str:
    s = str(value or "").strip()
    if not s:
        raise RuntimeError(f"{label}: missing platform model ref")
    if not _is_desifaces_platform_model_ref(s):
        raise RuntimeError(
            f"{label}: external human model refs are forbidden; "
            f"must be under az://{_platform_model_container_name()}/{_platform_model_prefix()}/... "
            f"or corresponding DesiFaces Azure Blob URL. got={s}"
        )
    return s


def _blob_name_is_full_body(blob_name: str) -> bool:
    low = (blob_name or "").lower()
    return any(t in low for t in ("fullbody", "full_body", "full-body"))


def _infer_gender_from_platform_model_blob_name(blob_name: str) -> str:
    low = (blob_name or "").lower()
    if "female" in low or "woman" in low or "women" in low or "girl" in low:
        return "female"
    if "male" in low or "/men/" in low or "/man/" in low or "boy" in low:
        return "male"
    return "any"


def _extract_model_code_from_platform_blob(blob_name: str) -> str:
    prefix = _platform_model_prefix().rstrip("/") + "/"
    name = str(blob_name or "").strip().lstrip("/")
    if not name.startswith(prefix):
        return ""
    rest = name[len(prefix) :]
    return rest.split("/", 1)[0].strip()


def _configured_default_platform_model_ref(
    *,
    bucket_gender: str,
    saree_like: bool,
) -> Tuple[str, str]:
    """
    Azure-only configured fallback refs.
    Primary behavior should come from Azure catalog random selection.
    These are safety-net fallbacks only.
    """
    bucket = _normalize_gender(bucket_gender)

    if saree_like:
        ref = (
            (os.getenv("COMMERCE_SAREE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
            or (os.getenv("COMMERCE_SAREE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
            or (os.getenv("COMMERCE_FEMALE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
            or (os.getenv("COMMERCE_FEMALE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
        )
        code = (
            (os.getenv("COMMERCE_SAREE_DEFAULT_PLATFORM_MODEL_CODE") or "").strip()
            or (os.getenv("COMMERCE_FEMALE_DEFAULT_PLATFORM_MODEL_CODE") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_CODE") or "").strip()
            or "default_saree_platform_model"
        )
        return ref, code

    if bucket == "female":
        ref = (
            (os.getenv("COMMERCE_FEMALE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
            or (os.getenv("COMMERCE_FEMALE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
        )
        code = (
            (os.getenv("COMMERCE_FEMALE_DEFAULT_PLATFORM_MODEL_CODE") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_CODE") or "").strip()
            or "default_female_platform_model"
        )
        return ref, code

    if bucket == "male":
        ref = (
            (os.getenv("COMMERCE_MALE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
            or (os.getenv("COMMERCE_MALE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
        )
        code = (
            (os.getenv("COMMERCE_MALE_DEFAULT_PLATFORM_MODEL_CODE") or "").strip()
            or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_CODE") or "").strip()
            or "default_male_platform_model"
        )
        return ref, code

    ref = (
        (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
        or (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
    )
    code = (
        (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_CODE") or "").strip()
        or "default_platform_model"
    )
    return ref, code


async def _list_azure_catalog_platform_models_best_effort(
    *,
    bucket_gender: str,
    require_full_body: bool,
) -> List[Dict[str, Any]]:
    """
    Enumerate only DesiFaces Azure catalog platform models:
      az://commerce-catalog/platform_models/<model_code>/source.jpg
    """
    conn = (
        (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
        or (os.getenv("COMMERCE_AZURE_STORAGE_CONNECTION_STRING") or "").strip()
        or (os.getenv("DF_AZURE_STORAGE_CONNECTION_STRING") or "").strip()
    )
    if not conn:
        logger.warning("commerce_processor: missing Azure connection string for platform-model catalog scan")
        return []

    container = _platform_model_container_name()
    prefix = _platform_model_prefix()
    source_file = _platform_model_source_file().lower()
    max_candidates = _platform_model_max_candidates()
    bucket = _normalize_gender(bucket_gender)

    def _scan() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        bsc = BlobServiceClient.from_connection_string(conn)
        cc = bsc.get_container_client(container)
        starts_with = prefix.rstrip("/") + "/"

        for blob in cc.list_blobs(name_starts_with=starts_with):
            name = str(blob.name or "").strip()
            low = name.lower()

            if not (low.endswith("/" + source_file) or low == source_file):
                continue

            if require_full_body and not _blob_name_is_full_body(low):
                continue

            gender = _infer_gender_from_platform_model_blob_name(low)
            if bucket in ("female", "male") and gender != bucket:
                continue

            model_code = _extract_model_code_from_platform_blob(name)
            if not model_code:
                continue

            out.append(
                {
                    "az_ref": f"az://{container}/{name}",
                    "blob_name": name,
                    "model_code": model_code,
                    "gender": gender,
                    "container": container,
                    "source_file": source_file,
                }
            )
            if len(out) >= max_candidates:
                break

        uniq = {str(x["az_ref"]): x for x in out}
        return sorted(uniq.values(), key=lambda x: str(x["az_ref"]))

    try:
        return await asyncio.to_thread(_scan)
    except Exception as e:
        logger.warning("commerce_processor: Azure catalog platform-model scan failed err=%s", e)
        return []


async def _pick_random_platform_model_from_azure_catalog(
    *,
    request_hash: str,
    quote_id: UUID,
    user_id: UUID,
    bucket_gender: str,
    saree_like: bool,
    require_full_body: bool,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Deterministic-random selection from DesiFaces Azure platform-model catalog.
    Falls back to configured Azure-only env refs if the catalog has zero eligible candidates.
    """
    bucket = _normalize_gender(bucket_gender)
    dbg: Dict[str, Any] = {
        "source": "azure_catalog_random",
        "selection_mode": "deterministic_random",
        "bucket_gender": bucket,
        "saree_like": bool(saree_like),
        "require_full_body": bool(require_full_body),
        "candidate_count": 0,
    }

    candidates = await _list_azure_catalog_platform_models_best_effort(
        bucket_gender=bucket,
        require_full_body=require_full_body,
    )
    dbg["candidate_count"] = len(candidates)

    storage = _get_storage_service_best_effort()
    sas_expires_in_s = _coerce_int(os.getenv("COMMERCE_VTON_SAS_EXPIRES_S"), 86400) or 86400

    if candidates:
        ordered = sorted(candidates, key=lambda x: str(x["az_ref"]))
        seed_material = f"{request_hash}:{quote_id}:{user_id}:{bucket}:{'saree' if saree_like else 'non_saree'}"
        idx = _stable_pick_index(seed_material=seed_material, n=len(ordered))
        chosen = ordered[idx]

        resolved_url = _resolve_platform_model_asset_url(
            storage=storage,
            url=str(chosen["az_ref"]),
            sas_expires_in_s=sas_expires_in_s,
        )
        if not _is_http_url(resolved_url):
            raise RuntimeError(
                f"commerce_processor: resolved Azure catalog platform model is not http(s): {resolved_url}"
            )

        selection = {
            "source": "azure_catalog_random",
            "selection_mode": "deterministic_random",
            "bucket_gender": bucket,
            "saree_like": bool(saree_like),
            "require_full_body": bool(require_full_body),
            "model_code": chosen.get("model_code"),
            "gender": chosen.get("gender") if bucket in ("female", "male") else chosen.get("gender"),
            "asset_az_ref": chosen.get("az_ref"),
            "primary_asset_url": resolved_url,
            "candidate_count": len(ordered),
            "candidate_index": idx,
            "quote_id": str(quote_id),
            "user_id": str(user_id),
            "request_hash": request_hash[:16],
        }
        dbg["selected"] = {
            "model_code": chosen.get("model_code"),
            "asset_az_ref": chosen.get("az_ref"),
            "primary_asset_url": resolved_url,
            "candidate_index": idx,
        }
        dbg["reason"] = "selected_from_catalog"
        return selection, dbg

    configured_ref, configured_code = _configured_default_platform_model_ref(
        bucket_gender=bucket,
        saree_like=saree_like,
    )
    if configured_ref:
        _assert_desifaces_platform_model_ref(
            value=configured_ref,
            label="commerce_processor.configured_default_platform_model",
        )

        resolved_url = _resolve_platform_model_asset_url(
            storage=storage,
            url=configured_ref,
            sas_expires_in_s=sas_expires_in_s,
        )
        if not _is_http_url(resolved_url):
            raise RuntimeError(
                f"commerce_processor: resolved configured default platform model is not http(s): {resolved_url}"
            )

        selection = {
            "source": "configured_default_platform_model",
            "selection_mode": "configured_default",
            "bucket_gender": bucket,
            "saree_like": bool(saree_like),
            "require_full_body": bool(require_full_body),
            "model_code": configured_code,
            "gender": bucket if bucket in ("female", "male") else None,
            "asset_az_ref": configured_ref if str(configured_ref).startswith("az://") else None,
            "primary_asset_url": resolved_url,
            "candidate_count": 1,
            "candidate_index": 0,
            "configured_ref": configured_ref,
            "quote_id": str(quote_id),
            "user_id": str(user_id),
            "request_hash": request_hash[:16],
        }
        dbg["selected"] = {
            "model_code": configured_code,
            "configured_ref": configured_ref,
            "primary_asset_url": resolved_url,
        }
        dbg["reason"] = "selected_from_configured_default"
        return selection, dbg

    dbg["reason"] = "no_catalog_candidates_and_no_configured_default"
    return None, dbg


def _apply_platform_model_selection_to_model_ref(
    *,
    model_ref: Dict[str, Any],
    selection: Dict[str, Any],
) -> Dict[str, Any]:
    mr = dict(model_ref or {})
    primary_asset_url = str(selection.get("primary_asset_url") or "").strip()
    if not _is_http_url(primary_asset_url):
        raise RuntimeError("commerce_processor: platform-model selection primary_asset_url is not http(s)")

    mr["human_image_url"] = primary_asset_url
    mr["url"] = primary_asset_url

    if selection.get("gender") and not mr.get("gender"):
        mr["gender"] = str(selection.get("gender"))

    meta2 = _as_dict(mr.get("meta"))
    meta2["platform_model_selection"] = selection
    meta2["platform_model_code"] = selection.get("model_code")

    if selection.get("gender") and not meta2.get("gender"):
        meta2["gender"] = selection.get("gender")

    views = _as_dict(meta2.get("views"))
    views["full_body"] = True
    meta2["views"] = views
    meta2["full_body"] = True
    meta2["is_full_body"] = True

    mr["meta"] = meta2
    mr["full_body"] = True
    mr["is_full_body"] = True
    return mr


def _resolve_existing_platform_model_ref_to_http(
    *,
    value: str,
) -> str:
    _assert_desifaces_platform_model_ref(
        value=value,
        label="commerce_processor.request_supplied_platform_model_ref",
    )
    storage = _get_storage_service_best_effort()
    sas_expires_in_s = _coerce_int(os.getenv("COMMERCE_VTON_SAS_EXPIRES_S"), 86400) or 86400
    resolved = _resolve_platform_model_asset_url(
        storage=storage,
        url=value,
        sas_expires_in_s=sas_expires_in_s,
    )
    if not _is_http_url(resolved):
        raise RuntimeError(
            f"commerce_processor: request-supplied platform model could not be resolved to http(s): {resolved}"
        )
    return resolved


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
    "kurta_pyjama",
    "kurta_set",
    "sherwani",
}


def _infer_target_gender(*, quote_request: Dict[str, Any], product_assets: Dict[str, Any]) -> str:
    qr = _as_dict(quote_request)
    pa = _as_dict(product_assets)

    if _looks_saree_like_for_platform_selector(product_assets=pa):
        return "female"

    g = qr.get("gender") or qr.get("target_gender") or _as_dict(qr.get("model_ref")).get("gender")
    if g:
        return _normalize_gender(g)

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

    if any(
        t in blob
        for t in (
            "hoodie",
            "blazer",
            "jacket",
            "coat",
            "overcoat",
            "sweater",
            "cardigan",
            "shirt",
            "tshirt",
            "t-shirt",
            "top",
            "kurta",
            "blouse",
            "choli",
        )
    ):
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


async def _inject_platform_model_for_saree_best_effort(
    *,
    quote_request: Dict[str, Any],
    product_assets: Dict[str, Any],
    model_ref: Dict[str, Any],
    request_hash: str,
    quote_id: UUID,
    user_id: UUID,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Saree vendor-only flow:
    if no explicit human image is present, inject a deterministic-random female full-body
    platform model from DesiFaces Azure catalog. Uses configured Azure-only env refs
    only as fallback when the catalog has zero eligible candidates.
    """
    mr = dict(model_ref or {})
    dbg: Dict[str, Any] = {
        "requested": False,
        "enabled": (
            os.getenv("COMMERCE_ENABLE_SAREE_PLATFORM_MODEL_AUTO_PICK") or "1"
        ).strip().lower() not in ("0", "false", "no"),
        "saree_like": False,
    }

    if not dbg["enabled"]:
        dbg["reason"] = "saree_auto_pick_disabled"
        return mr, dbg

    saree_like = _looks_saree_like_for_platform_selector(product_assets=product_assets)
    dbg["saree_like"] = bool(saree_like)
    if not saree_like:
        dbg["reason"] = "not_saree_like"
        return mr, dbg

    requested = _platform_mode_requested(
        quote_request=quote_request,
        product_assets=product_assets,
        model_ref=model_ref,
    )
    dbg["requested"] = bool(requested)

    force_when_missing_human = (
        os.getenv("COMMERCE_PLATFORM_MODEL_FORCE_WHEN_MISSING_HUMAN") or "1"
    ).strip().lower() not in ("0", "false", "no")
    dbg["force_when_missing_human"] = bool(force_when_missing_human)

    mr = _ensure_human_image_url(mr)
    existing = str(
        mr.get("human_image_url")
        or mr.get("url")
        or mr.get("image_url")
        or ""
    ).strip()

    if existing:
        existing_resolved = _resolve_existing_platform_model_ref_to_http(value=existing)
        mr["human_image_url"] = existing_resolved
        mr["url"] = existing_resolved

        meta2 = _as_dict(mr.get("meta"))
        meta2.setdefault(
            "platform_model_selection",
            {
                "source": "request_supplied_model_ref",
                "selection_mode": "request_supplied",
                "primary_asset_url": existing_resolved,
                "requested": bool(requested),
                "saree_like": True,
                "bucket_gender": "female",
                "quote_id": str(quote_id),
                "user_id": str(user_id),
                "request_hash": request_hash[:16],
            },
        )
        meta2["full_body"] = True
        meta2["is_full_body"] = True
        views = _as_dict(meta2.get("views"))
        views["full_body"] = True
        meta2["views"] = views
        mr["meta"] = meta2
        mr["full_body"] = True
        mr["is_full_body"] = True

        dbg["selection"] = {
            "source": "request_supplied_model_ref",
            "primary_asset_url": existing_resolved,
        }
        dbg["reason"] = "human_present"
        return mr, dbg

    if not requested and not force_when_missing_human:
        dbg["reason"] = "not_requested_and_missing_human"
        return mr, dbg

    selection, pick_dbg = await _pick_random_platform_model_from_azure_catalog(
        request_hash=request_hash,
        quote_id=quote_id,
        user_id=user_id,
        bucket_gender="female",
        saree_like=True,
        require_full_body=True,
    )
    dbg["candidate_count"] = pick_dbg.get("candidate_count")
    dbg["catalog_pick"] = pick_dbg

    if not selection:
        dbg["reason"] = "missing_default_platform_model_env_and_no_catalog_candidates"
        raise RuntimeError(
            "commerce_processor: saree vendor flow requires an eligible female full-body platform model "
            "from DesiFaces Azure catalog under az://commerce-catalog/platform_models/.../source.jpg, "
            "or an Azure-only fallback via COMMERCE_SAREE_DEFAULT_PLATFORM_MODEL_AZ / "
            "COMMERCE_SAREE_DEFAULT_PLATFORM_MODEL_URL "
            "(fallback: COMMERCE_FEMALE_DEFAULT_PLATFORM_MODEL_AZ / COMMERCE_DEFAULT_PLATFORM_MODEL_AZ)"
        )

    mr = _apply_platform_model_selection_to_model_ref(model_ref=mr, selection=selection)
    dbg["selection"] = {
        "model_code": selection.get("model_code"),
        "primary_asset_url": selection.get("primary_asset_url"),
        "asset_az_ref": selection.get("asset_az_ref"),
        "candidate_count": selection.get("candidate_count"),
        "candidate_index": selection.get("candidate_index"),
        "selection_mode": selection.get("selection_mode"),
    }
    dbg["reason"] = "selected"
    return mr, dbg


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
    Non-saree platform-model resolution.

    Primary:
      deterministic-random selection from DesiFaces Azure platform-model catalog
      using the resolved target-gender bucket (female / male / any).

    Fallback:
      existing approved selector path, but still enforce Azure-only platform-model assets.
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

    if human_url_existing:
        resolved_existing = _resolve_existing_platform_model_ref_to_http(value=human_url_existing)
        mr["human_image_url"] = resolved_existing
        mr["url"] = resolved_existing
        dbg["reason"] = "human_present"
        dbg["selection"] = {
            "source": "request_supplied_model_ref",
            "primary_asset_url": resolved_existing,
        }
        return mr, dbg

    if not requested and not (force_when_missing_human and not human_url_existing):
        dbg["reason"] = "not_requested_no_force_or_human_present"
        return mr, dbg

    garment_kind = _infer_non_saree_platform_garment_kind(product_assets=product_assets)
    dbg["resolved_garment_kind"] = garment_kind
    if not garment_kind:
        dbg["reason"] = "garment_kind_unresolved"
        return mr, dbg

    target_gender = _infer_target_gender(quote_request=quote_request, product_assets=product_assets)
    bucket_gender = target_gender if target_gender in ("female", "male") else "any"
    dbg["bucket_gender"] = bucket_gender

    random_selection, random_dbg = await _pick_random_platform_model_from_azure_catalog(
        request_hash=request_hash,
        quote_id=quote_id,
        user_id=user_id,
        bucket_gender=bucket_gender,
        saree_like=False,
        require_full_body=True,
    )
    dbg["catalog_pick"] = random_dbg

    if random_selection:
        mr = _apply_platform_model_selection_to_model_ref(model_ref=mr, selection=random_selection)
        dbg["selection"] = {
            "model_code": random_selection.get("model_code"),
            "gender": random_selection.get("gender"),
            "primary_asset_url": random_selection.get("primary_asset_url"),
            "asset_az_ref": random_selection.get("asset_az_ref"),
            "candidate_count": random_selection.get("candidate_count"),
            "candidate_index": random_selection.get("candidate_index"),
            "selection_mode": random_selection.get("selection_mode"),
        }
        dbg["reason"] = "selected_from_random_catalog"
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
    _assert_desifaces_platform_model_ref(
        value=selected_url,
        label="commerce_processor.non_saree_selector.primary_asset_url",
    )
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
    dbg["reason"] = "selected_from_selector_fallback"
    dbg["request_hash"] = request_hash[:16]
    return mr, dbg


# -----------------------------
# DB read / write helpers
# -----------------------------


async def _read_job_state(con, *, job_id: UUID) -> Dict[str, Any]:
    row = await con.fetchrow(
        """
        select payload_json, meta_json, computed_json, status, error_code, error_message
        from public.studio_jobs
        where id=$1 and studio_type='commerce'
        """,
        job_id,
    )
    if not row:
        return {
            "payload_json": {},
            "meta_json": {},
            "computed_json": {},
            "status": None,
            "error_code": None,
            "error_message": None,
        }
    return {
        "payload_json": _as_dict(row["payload_json"]),
        "meta_json": _as_dict(row["meta_json"]),
        "computed_json": _as_dict(row["computed_json"]),
        "status": row["status"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
    }


async def _write_job_state(
    con,
    *,
    job_id: UUID,
    payload: Dict[str, Any],
    meta: Dict[str, Any],
    computed: Dict[str, Any],
    status: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    await con.execute(
        """
        update public.studio_jobs
        set
          payload_json = $2::jsonb,
          meta_json = $3::jsonb,
          computed_json = $4::jsonb,
          status = coalesce($5, status),
          error_code = $6,
          error_message = $7,
          updated_at = now()
        where id = $1 and studio_type='commerce'
        """,
        job_id,
        json.dumps(payload or {}, default=str, ensure_ascii=False),
        json.dumps(meta or {}, default=str, ensure_ascii=False),
        json.dumps(computed or {}, default=str, ensure_ascii=False),
        status,
        error_code,
        error_message,
    )


async def _set_job_computed(
    con,
    *,
    job_id: UUID,
    stage: str,
    patch: Dict[str, Any] | None = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    state = await _read_job_state(con, job_id=job_id)
    payload = _as_dict(state.get("payload_json"))
    meta = _as_dict(state.get("meta_json"))
    computed = _as_dict(state.get("computed_json"))

    payload_computed = _as_dict(payload.get("computed"))
    merged_computed = dict(payload_computed)
    merged_computed.update(computed)
    merged_computed["stage"] = stage
    if patch:
        merged_computed.update(patch)

    payload["computed"] = merged_computed
    payload["stage"] = stage

    if "pricing" in merged_computed and isinstance(merged_computed["pricing"], dict):
        payload["pricing"] = merged_computed["pricing"]
        payload_meta = _as_dict(payload.get("meta"))
        payload_meta["pricing"] = merged_computed["pricing"]
        payload["meta"] = payload_meta

        meta["pricing"] = merged_computed["pricing"]
        if merged_computed["pricing"].get("state") is not None:
            meta["pricing_state"] = merged_computed["pricing"].get("state")
            payload["pricing_state"] = merged_computed["pricing"].get("state")
            merged_computed["pricing_state"] = merged_computed["pricing"].get("state")

    if stage in {"queued", "running", "succeeded", "failed"}:
        status_value = stage
    else:
        status_value = None

    await _write_job_state(
        con,
        job_id=job_id,
        payload=payload,
        meta=meta,
        computed=merged_computed,
        status=status_value,
        error_code=error_code if stage == "failed" else None,
        error_message=error_message if stage == "failed" else None,
    )


class _DisabledPricingClient:
    enabled = False

    async def commit(self, req: PricingCommitRequest):
        raise PricingClientError("pricing client unavailable")

    async def release(self, req: PricingReleaseRequest):
        raise PricingClientError("pricing client unavailable")


def _pricing_client() -> SvcPricingClient | _DisabledPricingClient:
    try:
        return SvcPricingClient.from_env(service_name="svc-commerce")
    except Exception:
        logger.exception("svc_commerce_pricing_client_init_failed")
        return _DisabledPricingClient()


def _pricing_enabled(client: Any) -> bool:
    try:
        return bool(getattr(client, "enabled", False))
    except Exception:
        return False


def _pricing_resp_get(resp: Any, key: str, default: Any = None) -> Any:
    if resp is None:
        return default
    if isinstance(resp, dict):
        value = resp.get(key, default)
    else:
        value = getattr(resp, key, default)
    if hasattr(value, "value"):
        try:
            return value.value
        except Exception:
            return default if value is None else value
    return value


def _merge_pricing_block(current: Optional[Dict[str, Any]], **updates: Any) -> Dict[str, Any]:
    out = dict(current or {})
    for key, value in updates.items():
        if value is not None:
            out[key] = value
    return out


def _extract_pricing_error_code(e: Exception) -> str:
    msg = str(e or "")
    for code in (
        "PRICING_INSUFFICIENT_CREDITS",
        "PRICING_UNKNOWN_OR_INACTIVE_VARIANT",
        "PRICING_VARIANT_ZERO_QTY_LINES",
        "PRICING_CLIENT_DISABLED",
    ):
        if code in msg:
            return code
    if "pricing client unavailable" in msg.lower():
        return "PRICING_CLIENT_DISABLED"
    return "PRICING_RESERVATION_FAILED"


async def _persist_pricing_block(con, *, job_id: UUID, pricing: Dict[str, Any]) -> None:
    state = await _read_job_state(con, job_id=job_id)
    payload = _as_dict(state.get("payload_json"))
    meta = _as_dict(state.get("meta_json"))
    computed = _as_dict(state.get("computed_json"))

    pricing = dict(pricing or {})
    pricing_state = pricing.get("state")

    payload["pricing"] = pricing
    payload["pricing_state"] = pricing_state

    payload_meta = _as_dict(payload.get("meta"))
    payload_meta["pricing"] = pricing
    if pricing_state is not None:
        payload_meta["pricing_state"] = pricing_state
    payload["meta"] = payload_meta

    payload_computed = _as_dict(payload.get("computed"))
    payload_computed["pricing"] = pricing
    if pricing_state is not None:
        payload_computed["pricing_state"] = pricing_state
    payload["computed"] = payload_computed

    meta["pricing"] = pricing
    if pricing_state is not None:
        meta["pricing_state"] = pricing_state

    computed["pricing"] = pricing
    if pricing_state is not None:
        computed["pricing_state"] = pricing_state

    await _write_job_state(
        con,
        job_id=job_id,
        payload=payload,
        meta=meta,
        computed=computed,
        status=None,
        error_code=state.get("error_code"),
        error_message=state.get("error_message"),
    )


async def _load_latest_pricing(con, *, job_id: UUID) -> Dict[str, Any]:
    state = await _read_job_state(con, job_id=job_id)
    payload = _as_dict(state.get("payload_json"))
    meta = _as_dict(state.get("meta_json"))
    computed = _as_dict(state.get("computed_json"))

    pricing = _as_dict(payload.get("pricing"))
    if pricing:
        return pricing

    pricing = _as_dict(_as_dict(payload.get("meta")).get("pricing"))
    if pricing:
        return pricing

    pricing = _as_dict(meta.get("pricing"))
    if pricing:
        return pricing

    pricing = _as_dict(computed.get("pricing"))
    if pricing:
        return pricing

    return {}


async def _commit_pricing_for_job(
    con,
    *,
    pricing_client: Any,
    job_id: UUID,
    user_id: UUID,
    pricing: Dict[str, Any],
    actual_units: int,
) -> Dict[str, Any]:
    if not _pricing_enabled(pricing_client):
        return pricing

    latest_pricing = await _load_latest_pricing(con, job_id=job_id)
    if latest_pricing:
        pricing = latest_pricing

    reservation_id = str(pricing.get("reservation_id") or "").strip()
    state = str(pricing.get("state") or "").strip().lower()

    if not reservation_id:
        pricing = _merge_pricing_block(
            pricing,
            state="commit_failed",
            actual_units=str(max(1, int(actual_units))),
            error="missing_reservation_id_at_commit",
        )
        await _persist_pricing_block(con, job_id=job_id, pricing=pricing)
        return pricing

    if state not in {"reserved", "commit_failed"}:
        return pricing

    variant_code = str(pricing.get("variant_code") or pricing.get("sku_code") or "").strip()
    leaf_sku_code = str(pricing.get("leaf_sku_code") or pricing.get("sku_code") or variant_code).strip()

    try:
        resp = await pricing_client.commit(
            PricingCommitRequest(
                user_id=str(user_id),
                reservation_id=reservation_id,
                actual_units=str(max(1, int(actual_units))),
                external_ref_type="studio_job",
                external_ref_id=str(job_id),
                idempotency_key=f"svc-commerce:job:{job_id}:commit",
                meta={
                    "variant_code": variant_code,
                    "sku_code": variant_code,
                    "leaf_sku_code": leaf_sku_code,
                    "service_action": pricing.get("service_action"),
                    "requested_units": pricing.get("estimated_units"),
                    "actual_units": str(max(1, int(actual_units))),
                    "quote_id": pricing.get("quote_id"),
                },
            )
        )

        commit_status = str(_pricing_resp_get(resp, "status", "committed") or "committed")
        pricing = _merge_pricing_block(
            pricing,
            state="committed",
            variant_code=_pricing_resp_get(resp, "variant_code") or variant_code,
            sku_code=_pricing_resp_get(resp, "variant_code") or variant_code,
            leaf_sku_code=_pricing_resp_get(resp, "sku_code") or leaf_sku_code,
            actual_units=str(max(1, int(actual_units))),
            commit_status=commit_status,
            reservation_status=commit_status,
            ledger_entry_id=_pricing_resp_get(resp, "ledger_entry_id"),
            billed_units=_pricing_resp_get(resp, "billed_units") or str(max(1, int(actual_units))),
            amount=_pricing_resp_get(resp, "amount"),
            currency=_pricing_resp_get(resp, "currency"),
            billing_mode=_pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode"),
            billing_account_id=_pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id"),
            settlement_mode=_pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode"),
            entitlement_source=_pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source"),
            disabled_reason=None,
            error=None,
        )
        await _persist_pricing_block(con, job_id=job_id, pricing=pricing)
        return pricing

    except Exception as e:
        logger.exception(
            "commerce_pricing_commit_failed",
            extra={"job_id": str(job_id), "reservation_id": reservation_id, "user_id": str(user_id)},
        )
        pricing = _merge_pricing_block(
            pricing,
            state="commit_failed",
            actual_units=str(max(1, int(actual_units))),
            error=str(e),
        )
        await _persist_pricing_block(con, job_id=job_id, pricing=pricing)
        return pricing


async def _release_pricing_for_job(
    con,
    *,
    pricing_client: Any,
    job_id: UUID,
    user_id: UUID,
    pricing: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    if not _pricing_enabled(pricing_client):
        return pricing

    latest_pricing = await _load_latest_pricing(con, job_id=job_id)
    if latest_pricing:
        pricing = latest_pricing

    reservation_id = str(pricing.get("reservation_id") or "").strip()
    state = str(pricing.get("state") or "").strip().lower()

    if not reservation_id:
        return pricing
    if state not in {"reserved", "release_failed"}:
        return pricing

    variant_code = str(pricing.get("variant_code") or pricing.get("sku_code") or "").strip()
    leaf_sku_code = str(pricing.get("leaf_sku_code") or pricing.get("sku_code") or variant_code).strip()

    try:
        resp = await pricing_client.release(
            PricingReleaseRequest(
                user_id=str(user_id),
                reservation_id=reservation_id,
                reason=reason,
                external_ref_type="studio_job",
                external_ref_id=str(job_id),
                idempotency_key=f"svc-commerce:job:{job_id}:release",
                meta={
                    "variant_code": variant_code,
                    "sku_code": variant_code,
                    "leaf_sku_code": leaf_sku_code,
                    "service_action": pricing.get("service_action"),
                    "requested_units": pricing.get("estimated_units"),
                    "quote_id": pricing.get("quote_id"),
                },
            )
        )

        release_status = str(_pricing_resp_get(resp, "status", "released") or "released")
        pricing = _merge_pricing_block(
            pricing,
            state="released",
            release_status=release_status,
            reservation_status=release_status,
            released_units=_pricing_resp_get(resp, "released_units") or pricing.get("estimated_units"),
            billing_mode=_pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode"),
            billing_account_id=_pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id"),
            settlement_mode=_pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode"),
            entitlement_source=_pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source"),
            disabled_reason=None,
            error=None,
        )
        await _persist_pricing_block(con, job_id=job_id, pricing=pricing)
        return pricing

    except Exception as e:
        logger.exception(
            "commerce_pricing_release_failed",
            extra={"job_id": str(job_id), "reservation_id": reservation_id, "user_id": str(user_id)},
        )
        pricing = _merge_pricing_block(pricing, state="release_failed", error=str(e))
        await _persist_pricing_block(con, job_id=job_id, pricing=pricing)
        return pricing


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
    best_score = -(10**9)
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

        self.min_upper = float(os.getenv("COMMERCE_VTON_QC_MIN_UPPER_DIFF") or "0.04")
        self.min_lower = float(os.getenv("COMMERCE_VTON_QC_MIN_LOWER_DIFF") or "0.04")
        self.min_both_upper = float(os.getenv("COMMERCE_VTON_QC_MIN_DRESS_UPPER_DIFF") or "0.03")
        self.min_both_lower = float(os.getenv("COMMERCE_VTON_QC_MIN_DRESS_LOWER_DIFF") or "0.05")

        self.max_face = float(os.getenv("COMMERCE_VTON_QC_MAX_FACE_DIFF") or "0.14")
        self.max_corners = float(os.getenv("COMMERCE_VTON_QC_MAX_CORNER_DIFF") or "0.15")

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

        import io

        from PIL import Image, ImageChops, ImageStat

        size = int(self.image_size)
        timeout_s = int(self.timeout_s)

        def _fetch(url: str) -> Image.Image:
            req = Request(url, headers={"User-Agent": "df-vton-qc"})
            raw = urlopen(req, timeout=timeout_s).read()
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            return im.resize((size, size))

        def _roi(im: Image.Image, x0: float, x1: float, y0: float, y1: float) -> Image.Image:
            w, h = im.size
            box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
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

        upper = _roi_diff(human, out, 0.10, 0.90, 0.18, 0.60)
        lower = _roi_diff(human, out, 0.08, 0.92, 0.60, 0.98)

        face = _roi_diff(human, out, 0.30, 0.70, 0.00, 0.28)

        c1 = _roi_diff(human, out, 0.00, 0.18, 0.00, 0.18)
        c2 = _roi_diff(human, out, 0.82, 1.00, 0.00, 0.18)
        c3 = _roi_diff(human, out, 0.00, 0.18, 0.82, 1.00)
        c4 = _roi_diff(human, out, 0.82, 1.00, 0.82, 1.00)
        corners = (c1 + c2 + c3 + c4) / 4.0

        gt = (garment_type or "upper_body").strip().lower()
        ok_presence = True
        ok_preserve = True

        if gt == "upper_body":
            ok_presence = upper >= self.min_upper
        elif gt == "lower_body":
            ok_presence = lower >= self.min_lower
        else:
            ok_presence = (upper >= self.min_both_upper) and (lower >= self.min_both_lower)

        if face > self.max_face or corners > self.max_corners:
            ok_preserve = False

        if gt == "upper_body" and lower > self.max_non_target:
            ok_preserve = False
        if gt == "lower_body" and upper > self.max_non_target:
            ok_preserve = False

        ok = bool(ok_presence and ok_preserve)
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
    pricing_client = _pricing_client()
    pricing = _as_dict(payload.get("pricing"))
    if not pricing:
        pricing = _as_dict(meta.get("pricing"))

    campaign_id: Optional[UUID] = None
    merged_meta: Dict[str, Any] = {}
    campaign_meta: Dict[str, Any] = {}

    async with pool.acquire() as con:
        persisted_pricing = await _load_latest_pricing(con, job_id=job_id)
        if persisted_pricing:
            pricing = persisted_pricing

        await _set_job_computed(
            con,
            job_id=job_id,
            stage="running",
            patch={
                "started_at": started_at,
                "processor": "vton_v3",
                "pricing": pricing if pricing else None,
                "pricing_state": pricing.get("state") if pricing else None,
            },
        )
        if pricing:
            await _persist_pricing_block(con, job_id=job_id, pricing=pricing)

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
                "pricing_state": pricing.get("state") if pricing else None,
                "pricing_enabled": bool(pricing.get("enabled", False)) if pricing else False,
                "pricing": pricing if pricing else None,
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
            quote_request=quote_request,
            payload=payload,
            quote_id=quote_id,
        )

        async with pool.acquire() as con:
            product_assets, dominant_code = await _apply_items_resolver_best_effort(con, product_assets=product_assets)

        product_assets = _merge_missing(product_assets, _as_dict(db_req.get("product_assets")))
        model_ref = _merge_missing(model_ref, _as_dict(db_req.get("model_ref")))

        product_assets = _ensure_garment_image_url(product_assets)
        model_ref = _ensure_human_image_url(model_ref)

        dominant_component_code = str(product_assets.get("dominant_component_code") or dominant_code or "").strip()

        if not str(product_assets.get("garment_type") or "").strip() and dominant_component_code:
            gt = _infer_garment_type_from_code(dominant_component_code)
            if gt:
                product_assets["garment_type"] = gt

        target_gender = _infer_target_gender(quote_request=quote_request, product_assets=product_assets)

        platform_pick_dbg: Dict[str, Any] = {}
        try:
            if _looks_saree_like_for_platform_selector(product_assets=product_assets):
                model_ref, platform_pick_dbg = await _inject_platform_model_for_saree_best_effort(
                    quote_request=quote_request,
                    product_assets=product_assets,
                    model_ref=model_ref,
                    request_hash=request_hash,
                    quote_id=quote_id,
                    user_id=user_id,
                )
            else:
                model_ref, platform_pick_dbg = await _preselect_platform_model_for_non_saree(
                    quote_request=quote_request,
                    product_assets=product_assets,
                    model_ref=model_ref,
                    request_hash=request_hash,
                    quote_id=quote_id,
                    user_id=user_id,
                )
        except Exception as e:
            raise RuntimeError(
                f"commerce_processor: platform model resolution failed err={type(e).__name__}: {e}"
            ) from e

        model_ref = _ensure_human_image_url(model_ref)

        product_assets, model_ref, full_body = _apply_full_body_hints(
            quote_request=quote_request,
            product_assets=product_assets,
            model_ref=model_ref,
        )

        garment_url = product_assets.get("garment_image_url")
        human_url = model_ref.get("human_image_url")

        if human_url:
            _assert_desifaces_platform_model_ref(
                value=human_url,
                label="commerce_processor.final_human_image_url",
            )
            human_url = _resolve_existing_platform_model_ref_to_http(value=str(human_url))
            model_ref["human_image_url"] = human_url
            model_ref["url"] = human_url

        provider = VTONProvider()

        must_have_inputs = bool(provider.enable_real and provider.provider == "fal" and not getattr(provider, "demo_mode", False))

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

        strict_gender = (os.getenv("COMMERCE_GENDER_COSTUME_STRICT") or "0").strip().lower() in ("1", "true", "yes", "y", "on")
        model_gender = _extract_gender_from_model_ref(model_ref)
        gender_policy_dbg = _apply_gender_policy_or_raise(
            target_gender=target_gender,
            model_gender=model_gender,
            dominant_component_code=dominant_component_code,
            strict=strict_gender,
        )

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
                patch={"request_hash": request_hash, "debug_inputs": debug_inputs, "pricing": pricing, "pricing_state": pricing.get("state") if pricing else None},
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

        result = await provider.generate(req)

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

        provider_platform_sel = _as_dict(provider_meta.get("platform_model_selection"))
        if provider_platform_sel:
            purl = str(provider_platform_sel.get("primary_asset_url") or "").strip()
            if purl:
                _assert_desifaces_platform_model_ref(
                    value=purl,
                    label="commerce_processor.provider_platform_model_selection.primary_asset_url",
                )
                if _is_http_url(purl):
                    human_url = purl
                    model_ref["human_image_url"] = purl
                    if not str(model_ref.get("url") or "").strip():
                        model_ref["url"] = purl
                    meta2 = _as_dict(model_ref.get("meta"))
                    meta2["platform_model_selection"] = provider_platform_sel
                    model_ref["meta"] = meta2
                    if provider_platform_sel.get("gender") and not model_ref.get("gender"):
                        model_ref["gender"] = str(provider_platform_sel.get("gender"))

        strict_variants = (os.getenv("COMMERCE_STRICT_VARIANT_TAGS") or "1").strip().lower() not in ("0", "false", "no")
        _validate_variant_urls_or_raise(
            job_id=job_id,
            expected_count=expected_variant_count,
            urls=urls,
            strict=strict_variants,
        )

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
            pricing = await _commit_pricing_for_job(
                con,
                pricing_client=pricing_client,
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
                actual_units=1,
            )
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
                    "pricing": pricing,
                    "pricing_state": pricing.get("state"),
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
                    "pricing_state": pricing.get("state"),
                    "pricing": pricing,
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
        code = _extract_pricing_error_code(e) if isinstance(e, PricingClientError) else type(e).__name__.upper()
        logger.exception("commerce_processor: job failed job_id=%s quote_id=%s", job_id, quote_id)
        failed_at = datetime.now(timezone.utc).isoformat()

        async with pool.acquire() as con:
            pricing = await _release_pricing_for_job(
                con,
                pricing_client=pricing_client,
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
                reason=code.lower(),
            )
            await _set_job_computed(
                con,
                job_id=job_id,
                stage="failed",
                patch={
                    "failed_at": failed_at,
                    "error": err[:2000],
                    "commerce_campaign_id": str(campaign_id) if campaign_id else None,
                    "quote_id": str(quote_id),
                    "pricing": pricing,
                    "pricing_state": pricing.get("state"),
                },
                error_code=code[:200] if code else None,
                error_message=err[:2000],
            )
            try:
                merged_meta_fail = _merge(
                    merged_meta,
                    {
                        "failed_at": failed_at,
                        "status": "failed",
                        "error": err[:2000],
                        "pricing_state": pricing.get("state"),
                        "pricing": pricing,
                    },
                )
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