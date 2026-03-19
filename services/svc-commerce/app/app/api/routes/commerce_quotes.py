from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import require_user
from app.db import get_pool
from app.domain.models import CommerceConfirmIn, CommerceConfirmOut, CommerceQuoteIn, CommerceQuoteOut
from app.services.azure_storage_service import AzureStorageService
from app.services.pricing_client import PricingClient

PRICING_IMPORT_ERROR: Optional[str] = None

try:
    from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
    from desifaces_shared.pricing.models import PricingReleaseRequest, PricingReserveRequest
except Exception as pricing_import_error:  # pragma: no cover
    PRICING_IMPORT_ERROR = str(pricing_import_error)

    class PricingClientError(Exception):
        pass

    @dataclass
    class PricingReserveRequest:
        user_id: str
        service_name: str
        service_action: str
        sku_code: str
        units: str
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

        async def reserve(self, req: PricingReserveRequest):
            raise PricingClientError("pricing client unavailable")

        async def release(self, req: PricingReleaseRequest):
            raise PricingClientError("pricing client unavailable")


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/commerce", tags=["commerce"])


# ---------------------------
# pricing helpers
# ---------------------------

class _DisabledPricingClient:
    enabled = False

    async def reserve(self, req: PricingReserveRequest):
        raise PricingClientError("pricing client unavailable")

    async def release(self, req: PricingReleaseRequest):
        raise PricingClientError("pricing client unavailable")


def _cfg_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _pricing_required() -> bool:
    return _cfg_bool("DF_PRICING_REQUIRED", False)


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


def _pricing_disabled_reason() -> str:
    if PRICING_IMPORT_ERROR:
        return f"pricing_import_failed: {PRICING_IMPORT_ERROR}"
    return "svc-commerce pricing client is disabled or not configured"


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


def _commerce_pricing_variant_code(*, product_type: str, request_json: Dict[str, Any]) -> str:
    dominant = str(
        _as_dict(request_json.get("product_assets")).get("dominant_component_code")
        or request_json.get("garment_kind")
        or request_json.get("outfit_kind")
        or ""
    ).strip().lower()

    explicit = (os.getenv("DF_PRICING_VARIANT_COMMERCE") or "").strip()
    if explicit:
        return explicit

    if product_type == "apparel":
        if "saree" in dominant:
            return (os.getenv("DF_PRICING_VARIANT_COMMERCE_SAREE") or "COMMERCE_VTON_SAREE_PREMIUM").strip()
        if "lehenga" in dominant:
            return (os.getenv("DF_PRICING_VARIANT_COMMERCE_APPAREL_PREMIUM") or "COMMERCE_VTON_APPAREL_PREMIUM").strip()
        return (os.getenv("DF_PRICING_VARIANT_COMMERCE_APPAREL") or "COMMERCE_VTON_APPAREL_STANDARD").strip()

    if product_type == "fmcg":
        return (os.getenv("DF_PRICING_VARIANT_COMMERCE_FMCG") or "COMMERCE_VTON_FMCG_STANDARD").strip()

    if product_type == "electronics":
        return (os.getenv("DF_PRICING_VARIANT_COMMERCE_ELECTRONICS") or "COMMERCE_VTON_ELECTRONICS_STANDARD").strip()

    return (os.getenv("DF_PRICING_VARIANT_COMMERCE_MIXED") or "COMMERCE_VTON_MULTI_ITEM").strip()


def _commerce_pricing_leaf_sku_code(*, product_type: str, request_json: Dict[str, Any]) -> str:
    dominant = str(
        _as_dict(request_json.get("product_assets")).get("dominant_component_code")
        or request_json.get("garment_kind")
        or request_json.get("outfit_kind")
        or ""
    ).strip().lower()

    explicit = (os.getenv("DF_PRICING_LEAF_SKU_COMMERCE") or "").strip()
    if explicit:
        return explicit

    if product_type == "apparel":
        if "saree" in dominant:
            return (os.getenv("DF_PRICING_LEAF_SKU_COMMERCE_SAREE") or "COMMERCE_VTON_RUN").strip()
        return (os.getenv("DF_PRICING_LEAF_SKU_COMMERCE_APPAREL") or "COMMERCE_VTON_RUN").strip()

    if product_type == "fmcg":
        return (os.getenv("DF_PRICING_LEAF_SKU_COMMERCE_FMCG") or "COMMERCE_VTON_RUN").strip()

    if product_type == "electronics":
        return (os.getenv("DF_PRICING_LEAF_SKU_COMMERCE_ELECTRONICS") or "COMMERCE_VTON_RUN").strip()

    return (os.getenv("DF_PRICING_LEAF_SKU_COMMERCE_MIXED") or "COMMERCE_VTON_RUN").strip()


def _build_initial_pricing_block(
    *,
    quote_id: UUID,
    request_json: Dict[str, Any],
    product_type: str,
    mode: str,
    resolution: str,
    total_credits: int,
    total_usd: float,
    total_inr: float,
    pricing_client: Any,
) -> Dict[str, Any]:
    variant_code = _commerce_pricing_variant_code(product_type=product_type, request_json=request_json)
    leaf_sku_code = _commerce_pricing_leaf_sku_code(product_type=product_type, request_json=request_json)

    enabled = _pricing_enabled(pricing_client)
    state = "pending_reservation" if enabled else "disabled"

    return {
        "enabled": enabled,
        "state": state,
        "service_name": "svc-commerce",
        "service_action": "commerce.vton.generate",
        "variant_code": variant_code,
        "sku_code": variant_code,
        "leaf_sku_code": leaf_sku_code,
        "estimated_units": "1",
        "unit_type": "run",
        "reservation_id": None,
        "reservation_status": None,
        "quote_id": str(quote_id),
        "reserved_units": None,
        "actual_units": None,
        "billed_units": None,
        "released_units": None,
        "amount": None,
        "currency": None,
        "ledger_entry_id": None,
        "billing_mode": None,
        "billing_account_id": None,
        "settlement_mode": None,
        "pricing_mode": None,
        "entitlement_source": None,
        "entitlement_reason": None,
        "tier_code": None,
        "disabled_reason": None if enabled else _pricing_disabled_reason(),
        "error": None,
        "meta": {
            "variant_code": variant_code,
            "leaf_sku_code": leaf_sku_code,
            "requested_units": "1",
            "mode": mode,
            "product_type": product_type,
            "resolution": resolution,
            "quote_id": str(quote_id),
            "quote_total_credits": int(total_credits),
            "quote_total_usd": float(total_usd),
            "quote_total_inr": float(total_inr),
            "dominant_component_code": _as_dict(request_json.get("product_assets")).get("dominant_component_code"),
            "garment_kind": request_json.get("garment_kind"),
            "outfit_kind": request_json.get("outfit_kind"),
        },
    }


def _merge_pricing_block(current: Optional[Dict[str, Any]], **updates: Any) -> Dict[str, Any]:
    out = dict(current or {})
    for key, value in updates.items():
        if value is not None:
            out[key] = value
    return out


def _pricing_from_payload_meta_computed(payload: Dict[str, Any], meta: Dict[str, Any], computed: Dict[str, Any]) -> Dict[str, Any]:
    pricing = _as_dict(payload.get("pricing"))
    if pricing:
        return pricing
    pricing = _as_dict(meta.get("pricing"))
    if pricing:
        return pricing
    pricing = _as_dict(computed.get("pricing"))
    if pricing:
        return pricing
    return {}


def _pricing_has_reservation(pricing: Dict[str, Any]) -> bool:
    rid = str(pricing.get("reservation_id") or "").strip()
    return bool(rid)


async def _release_pricing_best_effort(
    *,
    pricing_client: Any,
    user_id: UUID,
    studio_job_id: UUID,
    pricing: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    if not _pricing_enabled(pricing_client):
        return pricing

    reservation_id = str(pricing.get("reservation_id") or "").strip()
    state = str(pricing.get("state") or "").strip().lower()
    if not reservation_id:
        return pricing
    if state in {"released", "committed"}:
        return pricing

    try:
        resp = await pricing_client.release(
            PricingReleaseRequest(
                user_id=str(user_id),
                reservation_id=reservation_id,
                reason=reason,
                external_ref_type="studio_job",
                external_ref_id=str(studio_job_id),
                idempotency_key=f"svc-commerce:job:{studio_job_id}:release",
                meta={
                    **_as_dict(pricing.get("meta")),
                    "variant_code": pricing.get("variant_code"),
                    "sku_code": pricing.get("sku_code"),
                    "leaf_sku_code": pricing.get("leaf_sku_code"),
                    "service_action": pricing.get("service_action"),
                },
            )
        )
        return _merge_pricing_block(
            pricing,
            state="released",
            release_status=str(_pricing_resp_get(resp, "status", "released") or "released"),
            released_units=_pricing_resp_get(resp, "released_units"),
            billing_mode=_pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode"),
            billing_account_id=_pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id"),
            settlement_mode=_pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode"),
            entitlement_source=_pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source"),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "commerce_pricing_release_failed",
            extra={"studio_job_id": str(studio_job_id), "reservation_id": reservation_id, "user_id": str(user_id)},
        )
        return _merge_pricing_block(pricing, state="release_failed", error=str(e))


# ---------------------------
# small helpers (robust)
# ---------------------------

def _stable_hash(obj: Dict[str, Any]) -> str:
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


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


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (bytes, bytearray)):
        try:
            x = x.decode("utf-8", errors="ignore")
        except Exception:
            return {}
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)  # type: ignore[arg-type]
    except Exception:
        return {}


def _is_http_url(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith(("http://", "https://"))


def _normalize_urls(x: Any) -> List[str]:
    urls: List[str] = []
    if isinstance(x, str) and x.strip():
        urls = [x.strip()]
    elif isinstance(x, list):
        urls = [u.strip() for u in x if isinstance(u, str) and u.strip()]
    else:
        urls = []
    return [u for u in urls if u.lower().startswith(("http://", "https://"))]


def _merge_computed(payload_computed: Dict[str, Any], computed_col: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload_computed or {})
    out.update(computed_col or {})
    return out


def _normalize_job_status_payload(out: Dict[str, Any]) -> Dict[str, Any]:
    out = out if isinstance(out, dict) else {}
    computed = out.get("computed")
    computed = computed if isinstance(computed, dict) else {}
    urls = _normalize_urls(out.get("urls"))
    if not urls:
        urls = _normalize_urls(computed.get("urls"))
    out["urls"] = urls
    if "computed" not in out or not isinstance(out["computed"], dict):
        out["computed"] = computed
    out["computed"]["urls"] = urls
    out["preview_url"] = urls[0] if urls else None

    vc = out.get("variant_count")
    if not isinstance(vc, int) or vc <= 0:
        vc2 = computed.get("variant_count")
        if isinstance(vc2, int) and vc2 > 0:
            out["variant_count"] = vc2
        else:
            out["variant_count"] = len(urls)
    return out


def _b64url_encode_json(obj: Dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode_json(s: str) -> Dict[str, Any]:
    try:
        pad = "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode((s + pad).encode("utf-8"))
        j = json.loads(raw.decode("utf-8", errors="ignore"))
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def _pick_best_item_image_url(item: Dict[str, Any]) -> Optional[str]:
    v = item.get("image_url") or item.get("url")
    if _is_http_url(v):
        return str(v).strip()
    alts = item.get("image_urls")
    if isinstance(alts, list):
        for a in alts:
            if _is_http_url(a):
                return str(a).strip()
    return None


def _score_item(item: Dict[str, Any]) -> int:
    score = 0
    if bool(item.get("is_primary")):
        score += 10_000

    kind = str(item.get("kind") or "garment").strip().lower()
    if kind == "garment":
        score += 1_000
    elif kind in ("accessory", "jewelry"):
        score -= 250

    r = _coerce_int(item.get("dominance_rank"), default=9999)
    score += max(0, 500 - r)
    return score


def _extract_wrapped_request(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    for k in ("quote_request", "request", "input"):
        inner = raw.get(k)
        if isinstance(inner, dict) and inner:
            merged = dict(raw)
            merged.update(inner)
            return merged
    return dict(raw)


def _extract_confirm_patch(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    for k in ("quote_request", "request", "input"):
        inner = raw.get(k)
        if isinstance(inner, dict) and inner:
            return dict(inner)

    allowed = {
        "mode",
        "provider_kind",
        "product_type",
        "resolution",
        "language",
        "count",
        "outputs",
        "views",
        "cta",
        "product_assets",
        "model_ref",
        "drape_styles",
        "outfit_kind",
        "garment_kind",
        "people",
        "meta",
        "drape_style",
        "product_ids",
        "look_set_ids",
    }
    patch: Dict[str, Any] = {}
    for k in allowed:
        if k in raw:
            patch[k] = raw.get(k)
    return patch


def _deep_merge_request_json(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base or {})
    patch = dict(patch or {})

    for k in ("product_assets", "model_ref", "outputs", "views", "cta", "meta"):
        if isinstance(out.get(k), dict) and isinstance(patch.get(k), dict):
            merged = dict(out[k])
            merged.update(patch[k])
            patch[k] = merged

    out.update(patch)
    return out


def _infer_people_from_request_json(request_json: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(request_json or {})
    mr = _as_dict(d.get("model_ref"))
    meta = _as_dict(d.get("meta"))

    probe = " ".join(
        str(x or "")
        for x in [
            mr.get("human_image_url"),
            mr.get("image_url"),
            mr.get("url"),
            mr.get("photo_url"),
            mr.get("platform_model_id"),
            meta.get("model_code"),
        ]
    ).lower()

    if "male_" in probe or "/male_" in probe:
        d["people"] = ["solo_male"]
    elif "female_" in probe or "/female_" in probe:
        d["people"] = ["solo_female"]
    elif not isinstance(d.get("people"), list) or not d.get("people"):
        d["people"] = ["solo"]

    return d


def _normalize_vton_request_json(request_json: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(request_json or {})

    pa = dict(_as_dict(d.get("product_assets")))
    mr = dict(_as_dict(d.get("model_ref")))
    meta = dict(_as_dict(d.get("meta")))

    items = pa.get("items")
    if not isinstance(items, list):
        items = []

    best_item: Optional[Dict[str, Any]] = None
    best_score = -(10**9)
    for it in items:
        if not isinstance(it, dict):
            continue
        url = _pick_best_item_image_url(it)
        if not url:
            continue
        score = _score_item(it)
        if score > best_score:
            best_score = score
            best_item = it

    dominant_component_code = pa.get("dominant_component_code")
    garment_kind = d.get("garment_kind")
    outfit_kind = d.get("outfit_kind")

    if isinstance(best_item, dict):
        if not dominant_component_code and best_item.get("component_code"):
            dominant_component_code = best_item.get("component_code")
        if not garment_kind and best_item.get("garment_kind"):
            garment_kind = best_item.get("garment_kind")

    if not garment_kind and isinstance(dominant_component_code, str) and dominant_component_code.strip():
        garment_kind = dominant_component_code.strip()

    if not outfit_kind and garment_kind:
        outfit_kind = garment_kind

    if isinstance(best_item, dict):
        if garment_kind and not best_item.get("garment_kind"):
            best_item["garment_kind"] = garment_kind
        if not best_item.get("is_primary"):
            best_item["is_primary"] = True

    if best_item:
        updated_items: List[Dict[str, Any]] = []
        replaced = False
        for it in items:
            if it is best_item and isinstance(it, dict):
                updated_items.append(best_item)
                replaced = True
            else:
                updated_items.append(it)
        if not replaced:
            updated_items.insert(0, best_item)
        pa["items"] = updated_items

    if dominant_component_code:
        pa["dominant_component_code"] = dominant_component_code

    if best_item:
        item_url = _pick_best_item_image_url(best_item)
        if item_url and not _is_http_url(pa.get("garment_image_url")):
            pa["garment_image_url"] = item_url
        if item_url and not _is_http_url(pa.get("primary_image_url")):
            pa["primary_image_url"] = item_url

    d["garment_kind"] = garment_kind
    d["outfit_kind"] = outfit_kind
    d["product_assets"] = pa
    d["model_ref"] = mr
    d["meta"] = meta

    if not d.get("provider_kind") and d.get("mode"):
        d["provider_kind"] = d.get("mode")

    d = _infer_people_from_request_json(d)
    return d


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


def _default_platform_model_url(storage: AzureStorageService) -> Optional[str]:
    u = (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_URL") or "").strip()
    if _is_http_url(u):
        return u

    az = (os.getenv("COMMERCE_DEFAULT_PLATFORM_MODEL_AZ") or "").strip()
    got = _parse_az_ref(az) if az else None
    if got:
        c, b = got
        try:
            return storage.get_blob_sas_url(container=c, blob_name=b, expires_in_s=3600, permission="r")
        except Exception:
            pass

    fallback = "az://commerce-training/pools/20260222_165920_e8aa84d6/persons/000000_877386944.png"
    got2 = _parse_az_ref(fallback)
    if got2:
        c, b = got2
        try:
            return storage.get_blob_sas_url(container=c, blob_name=b, expires_in_s=3600, permission="r")
        except Exception:
            return None
    return None


def _component_code_from_role(role: str) -> str:
    r = (role or "").lower()
    if "saree" in r:
        return "saree"
    if "blouse" in r:
        return "blouse"
    if "shirt" in r:
        return "shirt"
    if "pant" in r:
        return "pants"
    if "dress" in r:
        return "dress"
    if "jewelry" in r or "earring" in r or "necklace" in r:
        return "jewelry"
    if "shoe" in r:
        return "shoes"
    if "handbag" in r:
        return "handbag"
    return "other"


def _resolve_vton_inputs_from_request_json(
    request_json: Dict[str, Any],
    *,
    storage: Optional[AzureStorageService] = None,
) -> Dict[str, Any]:
    pa = dict(_as_dict(request_json.get("product_assets")))
    mr = dict(_as_dict(request_json.get("model_ref")))

    dominant_code: Optional[str] = None
    garment_url: Optional[str] = None

    items = pa.get("items")
    if isinstance(items, list) and items:
        best: Optional[Dict[str, Any]] = None
        best_score = -(10**9)
        for it in items:
            if not isinstance(it, dict):
                continue
            u = _pick_best_item_image_url(it)
            if not u:
                continue
            s = _score_item(it)
            if s > best_score:
                best_score = s
                best = it

        if best:
            garment_url = _pick_best_item_image_url(best)
            cc = best.get("component_code")
            if isinstance(cc, str) and cc.strip():
                dominant_code = cc.strip()

    if not garment_url:
        for k in (
            "garment_image_url",
            "product_image_url",
            "primary_image_url",
            "saree_image_url",
            "blouse_image_url",
        ):
            v = pa.get(k)
            if _is_http_url(v):
                garment_url = str(v).strip()
                break

    human_url = mr.get("human_image_url")
    if not _is_http_url(human_url):
        for k in ("image_url", "url", "ref_url", "photo_url"):
            v = mr.get(k)
            if _is_http_url(v):
                human_url = str(v).strip()
                break

    mode = str(request_json.get("mode") or "").strip() or "platform_models"
    if mode == "platform_models" and not _is_http_url(human_url):
        if storage is None:
            storage = AzureStorageService()
        auto = _default_platform_model_url(storage)
        if _is_http_url(auto):
            human_url = auto
            mr["human_image_url"] = auto

    if garment_url:
        pa["garment_image_url"] = garment_url
    if dominant_code:
        pa["dominant_component_code"] = dominant_code
    if _is_http_url(human_url):
        mr["human_image_url"] = str(human_url).strip()

    if "garment_type" not in pa:
        url_l = (garment_url or "").lower()
        if (
            pa.get("saree_image_url")
            or "saree" in url_l
            or "lehenga" in url_l
            or "anarkali" in url_l
            or "gown" in url_l
            or "dress" in url_l
        ):
            pa["garment_type"] = "dresses"

    resolved_json = {
        "product_assets": pa,
        "model_ref": mr,
        "dominant_component_code": dominant_code,
        "resolved_garment_image_url": garment_url,
        "resolved_human_image_url": human_url,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "source": "quote_route",
    }

    return {
        "product_assets": pa,
        "model_ref": mr,
        "dominant_component_code": dominant_code,
        "resolved_garment_image_url": garment_url,
        "resolved_human_image_url": human_url,
        "resolved_json": resolved_json,
    }


async def _signed_url_for_media_asset(
    *,
    con,
    storage: AzureStorageService,
    asset_id: UUID,
    expected_user_id: UUID,
    expires_in_s: int = 3600,
) -> str:
    row = await con.fetchrow(
        "select id, user_id, storage_ref from public.media_assets where id=$1",
        asset_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"media_asset_not_found: {asset_id}")
    if UUID(str(row["user_id"])) != expected_user_id:
        raise HTTPException(status_code=403, detail=f"media_asset_not_owned_by_user: {asset_id}")

    storage_ref = str(row["storage_ref"] or "")
    if storage_ref.startswith("az://"):
        got = _parse_az_ref(storage_ref)
        if not got:
            raise HTTPException(status_code=422, detail=f"invalid_storage_ref_for_asset: {asset_id}")
        c, b = got
        return storage.get_blob_sas_url(container=c, blob_name=b, expires_in_s=expires_in_s, permission="r")

    if "/" in storage_ref and not storage_ref.startswith("http"):
        c, b = storage_ref.split("/", 1)
        return storage.get_blob_sas_url(container=c, blob_name=b, expires_in_s=expires_in_s, permission="r")

    if _is_http_url(storage_ref):
        return storage_ref

    raise HTTPException(status_code=422, detail=f"unsupported_storage_ref_for_asset: {asset_id}")


async def _expand_product_ids_into_product_assets(
    *,
    con,
    storage: AzureStorageService,
    request_json: Dict[str, Any],
    user_id: UUID,
) -> None:
    product_ids = request_json.get("product_ids")
    if not isinstance(product_ids, list) or not product_ids:
        return

    pa = dict(_as_dict(request_json.get("product_assets")))
    items = pa.get("items")
    if not isinstance(items, list):
        items = []
    role_urls = dict(_as_dict(pa.get("meta", {})).get("role_urls") if isinstance(pa.get("meta"), dict) else {})

    for pid in product_ids:
        try:
            pid_u = UUID(str(pid))
        except Exception:
            continue

        owner = await con.fetchval("select user_id from public.commerce_products where id=$1", pid_u)
        if owner is None:
            raise HTTPException(status_code=404, detail=f"product_not_found: {pid_u}")
        if UUID(str(owner)) != user_id:
            raise HTTPException(status_code=403, detail=f"product_not_owned_by_user: {pid_u}")

        rows = await con.fetch(
            """
            select asset_type, media_asset_id, meta_json
            from public.commerce_product_assets
            where product_id=$1
            order by created_at asc
            """,
            pid_u,
        )

        for r in rows:
            role = str(r["asset_type"] or "").strip()
            if not role:
                continue
            ma_id = r["media_asset_id"]
            if not ma_id:
                continue

            url = await _signed_url_for_media_asset(
                con=con,
                storage=storage,
                asset_id=UUID(str(ma_id)),
                expected_user_id=user_id,
            )
            role_urls[role] = url

            already = False
            for it in items:
                if isinstance(it, dict):
                    m = _as_dict(it.get("meta"))
                    if str(m.get("asset_type") or "") == role and _is_http_url(it.get("image_url")):
                        already = True
                        break
            if not already:
                cc = _component_code_from_role(role)
                kind = "garment" if cc in ("saree", "blouse", "shirt", "pants", "dress") else "other"
                items.append(
                    {
                        "component_code": cc,
                        "garment_kind": cc,
                        "kind": kind,
                        "image_url": url,
                        "image_urls": [url],
                        "is_primary": True if cc == "saree" and ("saree" in role.lower()) else False,
                        "meta": {"asset_type": role, "media_asset_id": str(ma_id), "source": "product_ids_expand"},
                    }
                )

    saree_url = role_urls.get("saree_full") or role_urls.get("saree")
    blouse_url = role_urls.get("blouse_piece") or role_urls.get("blouse")
    pallu_url = role_urls.get("pallu_full")
    border_url = role_urls.get("border_closeup")

    if saree_url and not _is_http_url(pa.get("saree_image_url")):
        pa["saree_image_url"] = saree_url
    if blouse_url and not _is_http_url(pa.get("blouse_image_url")):
        pa["blouse_image_url"] = blouse_url

    meta = dict(_as_dict(pa.get("meta")))
    meta["role_urls"] = role_urls
    if pallu_url:
        meta.setdefault("pallu_url", pallu_url)
    if border_url:
        meta.setdefault("border_url", border_url)
    pa["meta"] = meta
    pa["items"] = items

    request_json["product_assets"] = pa


async def _resolve_model_asset_id_to_url(
    *,
    con,
    storage: AzureStorageService,
    request_json: Dict[str, Any],
    user_id: UUID,
) -> None:
    mr = dict(_as_dict(request_json.get("model_ref")))
    asset_id = mr.get("asset_id")
    if not asset_id:
        request_json["model_ref"] = mr
        return
    if _is_http_url(mr.get("human_image_url")):
        request_json["model_ref"] = mr
        return
    try:
        aid = UUID(str(asset_id))
    except Exception:
        raise HTTPException(status_code=422, detail="model_ref.asset_id is not a valid UUID")

    url = await _signed_url_for_media_asset(con=con, storage=storage, asset_id=aid, expected_user_id=user_id)
    mr["human_image_url"] = url
    request_json["model_ref"] = mr


async def _ensure_confirm_request_has_urls(
    *,
    con,
    storage: AzureStorageService,
    request_json: Dict[str, Any],
    user_id: UUID,
) -> Dict[str, Any]:
    await _expand_product_ids_into_product_assets(
        con=con,
        storage=storage,
        request_json=request_json,
        user_id=user_id,
    )
    await _resolve_model_asset_id_to_url(
        con=con,
        storage=storage,
        request_json=request_json,
        user_id=user_id,
    )

    resolved = _resolve_vton_inputs_from_request_json(request_json, storage=storage)
    request_json["product_assets"] = resolved.get("product_assets") or request_json.get("product_assets")
    request_json["model_ref"] = resolved.get("model_ref") or request_json.get("model_ref")
    return resolved


def _require_vton_inputs_or_422(
    *,
    request_json: Dict[str, Any],
    storage: AzureStorageService,
) -> Dict[str, Any]:
    resolved = _resolve_vton_inputs_from_request_json(request_json, storage=storage)
    garment_url = resolved.get("resolved_garment_image_url")
    human_url = resolved.get("resolved_human_image_url")

    missing: List[str] = []
    if not _is_http_url(garment_url):
        missing.append("product_assets.items[].image_url OR product_assets.garment_image_url OR product_assets.saree_image_url")
    if not _is_http_url(human_url):
        missing.append("model_ref.human_image_url OR model_ref.image_url OR (mode=platform_models uses default model)")

    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "missing_vton_inputs",
                "missing": missing,
                "hint": (
                    "Vendor flow: set mode=platform_models and provide garment URL only; backend auto-picks "
                    "default model. Customer flow: provide model_ref.human_image_url (or model_ref.asset_id). "
                    "Wrapper bodies {request:{...}} and {quote_request:{...}} are supported."
                ),
            },
        )
    return resolved


# ---------------------------
# API
# ---------------------------

@router.post("/quote", response_model=CommerceQuoteOut, operation_id="commerce_quote_create")
async def quote(req: CommerceQuoteIn, request: Request, user_id: UUID = Depends(require_user)) -> CommerceQuoteOut:
    raw: Dict[str, Any] = {}
    try:
        raw_j = await request.json()
        raw = _as_dict(raw_j)
    except Exception:
        raw = {}

    normalized = _extract_wrapped_request(raw) if raw else req.model_dump(mode="json")
    normalized = _normalize_vton_request_json(normalized)

    try:
        req2 = CommerceQuoteIn.model_validate(normalized)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"invalid_quote_request: {type(e).__name__}: {e}") from e

    pc = PricingClient()
    out = await pc.quote(user_id=user_id, req=req2)

    pool = await get_pool()
    req_json = req2.model_dump(mode="json")
    req_json = _deep_merge_request_json(req_json, normalized)
    req_json = _normalize_vton_request_json(req_json)

    out_json = out.model_dump(mode="json")

    storage = AzureStorageService()
    resolved = _resolve_vton_inputs_from_request_json(req_json, storage=storage)

    total_usd = float(out.totals.get("usd", 0.0))
    total_inr = float(out.totals.get("inr", 0.0))

    mode = str(req_json.get("mode") or "").strip() or "platform_models"
    resolution = str(req_json.get("resolution") or "").strip() or "hd"

    async with pool.acquire() as con:
        await con.execute(
            """
            insert into public.commerce_quotes(
                id, user_id, scope, request_json, response_json,
                total_credits, total_usd, total_inr, status, expires_at,
                mode, resolution,
                resolved_json, dominant_component_code, resolved_garment_image_url, resolved_human_image_url,
                created_at, updated_at
            )
            values(
                $1, $2, 'commerce', $3::jsonb, $4::jsonb,
                $5, $6, $7, 'quoted', $8,
                $9, $10,
                $11::jsonb, $12, $13, $14,
                now(), now()
            )
            on conflict (id) do update
              set request_json = excluded.request_json,
                  response_json = excluded.response_json,
                  total_credits = excluded.total_credits,
                  total_usd = excluded.total_usd,
                  total_inr = excluded.total_inr,
                  status = excluded.status,
                  expires_at = excluded.expires_at,
                  mode = excluded.mode,
                  resolution = excluded.resolution,
                  resolved_json = excluded.resolved_json,
                  dominant_component_code = excluded.dominant_component_code,
                  resolved_garment_image_url = excluded.resolved_garment_image_url,
                  resolved_human_image_url = excluded.resolved_human_image_url,
                  updated_at = now()
            """,
            out.quote_id,
            user_id,
            json.dumps(req_json),
            json.dumps(out_json),
            int(out.total_credits),
            total_usd,
            total_inr,
            out.expires_at,
            mode,
            resolution,
            json.dumps(resolved.get("resolved_json") or {}),
            resolved.get("dominant_component_code"),
            resolved.get("resolved_garment_image_url"),
            resolved.get("resolved_human_image_url"),
        )

    return out


@router.post("/confirm", response_model=CommerceConfirmOut, operation_id="commerce_quote_confirm")
async def confirm(req: CommerceConfirmIn, request: Request, user_id: UUID = Depends(require_user)) -> CommerceConfirmOut:
    pool = await get_pool()
    now = datetime.now(timezone.utc)

    raw: Dict[str, Any] = {}
    try:
        raw_j = await request.json()
        raw = _as_dict(raw_j)
    except Exception:
        raw = {}

    idem_from_raw = raw.get("idempotency_key") if isinstance(raw, dict) else None
    idempotency_key = req.idempotency_key or (
        str(idem_from_raw).strip()
        if isinstance(idem_from_raw, str) and idem_from_raw.strip()
        else None
    )

    pricing_client = _pricing_client()

    existing_campaign = None
    existing_job_id: Optional[UUID] = None
    existing_job_status: Optional[str] = None
    request_json: Dict[str, Any] = {}
    mode = "platform_models"
    product_type = "apparel"
    resolution = "hd"
    request_hash = ""
    total_credits = 0
    total_usd = 0.0
    total_inr = 0.0
    storage = AzureStorageService()

    async with pool.acquire() as con:
        q = await con.fetchrow(
            """
            select
              id,
              user_id,
              status,
              expires_at,
              request_json,
              response_json,
              mode,
              resolution,
              total_credits,
              total_usd,
              total_inr,
              dominant_component_code,
              resolved_garment_image_url,
              resolved_human_image_url,
              resolved_json
            from public.commerce_quotes
            where id = $1 and user_id = $2
            """,
            req.quote_id,
            user_id,
        )

        if not q:
            raise HTTPException(status_code=404, detail="quote_not_found")

        if q["expires_at"] <= now:
            raise HTTPException(status_code=422, detail="quote_expired")

        if q["status"] not in ("quoted", "confirmed"):
            raise HTTPException(status_code=422, detail=f"quote_not_confirmable: {q['status']}")

        request_json = _as_dict(q["request_json"])
        request_json.setdefault("product_assets", {})
        request_json.setdefault("model_ref", {})
        request_json.setdefault("meta", {})

        patch = _extract_confirm_patch(raw)
        if patch:
            request_json = _deep_merge_request_json(request_json, patch)

        if getattr(req, "quote_request", None):
            request_json = _deep_merge_request_json(request_json, _as_dict(req.quote_request))
        if getattr(req, "request", None):
            request_json = _deep_merge_request_json(request_json, _as_dict(req.request))

        if not request_json.get("mode") and q.get("mode"):
            request_json["mode"] = q["mode"]
        if not request_json.get("resolution") and q.get("resolution"):
            request_json["resolution"] = q["resolution"]
        if getattr(req, "product_type", None) and not request_json.get("product_type"):
            request_json["product_type"] = req.product_type
        if getattr(req, "mode", None) and not request_json.get("mode"):
            request_json["mode"] = req.mode
        if getattr(req, "resolution", None) and not request_json.get("resolution"):
            request_json["resolution"] = req.resolution

        request_json = _normalize_vton_request_json(request_json)

        resolved = await _ensure_confirm_request_has_urls(
            con=con,
            storage=storage,
            request_json=request_json,
            user_id=user_id,
        )
        _require_vton_inputs_or_422(request_json=request_json, storage=storage)

        mode = str(
            request_json.get("mode")
            or getattr(req, "mode", None)
            or q.get("mode")
            or "platform_models"
        ).strip() or "platform_models"

        product_type = str(
            request_json.get("product_type")
            or getattr(req, "product_type", None)
            or "apparel"
        ).strip() or "apparel"

        resolution = str(
            request_json.get("resolution")
            or getattr(req, "resolution", None)
            or q.get("resolution")
            or "hd"
        ).strip() or "hd"

        total_credits = int(q.get("total_credits") or 0)
        total_usd = float(q.get("total_usd") or 0.0)
        total_inr = float(q.get("total_inr") or 0.0)

        idem_key = (idempotency_key or str(req.quote_id)).strip()
        request_hash = _stable_hash(
            {
                "kind": "commerce_confirm",
                "user_id": str(user_id),
                "quote_id": str(req.quote_id),
                "idempotency_key": idem_key,
                "mode": mode,
                "product_type": product_type,
                "resolution": resolution,
                "dominant_component_code": resolved.get("dominant_component_code"),
                "garment_url": str(resolved.get("resolved_garment_image_url") or "").split("?", 1)[0],
                "human_url": str(resolved.get("resolved_human_image_url") or "").split("?", 1)[0],
            }
        )

        existing_campaign = await con.fetchrow(
            """
            select id, meta_json
            from public.commerce_campaigns
            where user_id = $1 and quote_id = $2
              and (meta_json->>'request_hash') = $3
            order by created_at desc
            limit 1
            """,
            user_id,
            req.quote_id,
            request_hash,
        )

        if existing_campaign:
            mj = _as_dict(existing_campaign["meta_json"])
            sj = mj.get("studio_job_id")
            if sj:
                try:
                    existing_job_id = UUID(str(sj))
                except Exception:
                    existing_job_id = None

        if existing_job_id is None:
            row = await con.fetchrow(
                """
                select id, status, payload_json, meta_json, computed_json
                from public.studio_jobs
                where user_id = $1 and studio_type = 'commerce' and request_hash = $2
                order by created_at desc
                limit 1
                """,
                user_id,
                request_hash,
            )
            if row:
                row_payload = _as_dict(row["payload_json"])
                row_meta = _as_dict(row["meta_json"])
                row_computed = _as_dict(row["computed_json"])
                row_pricing = _pricing_from_payload_meta_computed(row_payload, row_meta, row_computed)

                if _pricing_required():
                    # do not dedupe against an old/unpriced broken job
                    if _pricing_has_reservation(row_pricing) or str(row["status"] or "").lower() in {
                        "failed",
                        "released",
                        "committed",
                        "succeeded",
                    }:
                        existing_job_id = UUID(str(row["id"]))
                        existing_job_status = str(row["status"] or "queued")
                else:
                    existing_job_id = UUID(str(row["id"]))
                    existing_job_status = str(row["status"] or "queued")

        if existing_job_id:
            return CommerceConfirmOut(
                campaign_id=UUID(str(existing_campaign["id"])) if existing_campaign else uuid4(),
                studio_job_id=existing_job_id,
                status=existing_job_status or "queued",
            )

    studio_job_id = uuid4()
    campaign_id = UUID(str(existing_campaign["id"])) if existing_campaign else uuid4()

    pricing = _build_initial_pricing_block(
        quote_id=req.quote_id,
        request_json=request_json,
        product_type=product_type,
        mode=mode,
        resolution=resolution,
        total_credits=total_credits,
        total_usd=total_usd,
        total_inr=total_inr,
        pricing_client=pricing_client,
    )

    if not _pricing_enabled(pricing_client):
        if _pricing_required():
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "pricing_disabled",
                    "code": "PRICING_CLIENT_DISABLED",
                    "message": _pricing_disabled_reason(),
                },
            )
    else:
        try:
            reserve_req = PricingReserveRequest(
                user_id=str(user_id),
                service_name="svc-commerce",
                service_action="commerce.vton.generate",
                sku_code=str(pricing.get("variant_code") or pricing.get("sku_code") or ""),
                units=str(pricing.get("estimated_units") or "1"),
                external_ref_type="studio_job",
                external_ref_id=str(studio_job_id),
                idempotency_key=f"svc-commerce:job:{studio_job_id}:reserve",
                meta={
                    **_as_dict(pricing.get("meta")),
                    "variant_code": pricing.get("variant_code"),
                    "sku_code": pricing.get("variant_code"),
                    "leaf_sku_code": pricing.get("leaf_sku_code"),
                    "service_action": pricing.get("service_action"),
                    "quote_id": str(req.quote_id),
                    "request_hash": request_hash,
                },
            )
            reserve_resp = await pricing_client.reserve(reserve_req)
            reserve_status = str(_pricing_resp_get(reserve_resp, "status", "reserved") or "reserved")
            if reserve_status.lower() == "disabled" and _pricing_required():
                raise PricingClientError("PRICING_CLIENT_DISABLED")

            pricing = _merge_pricing_block(
                pricing,
                state="reserved" if reserve_status.lower() != "disabled" else "disabled",
                reservation_status=reserve_status,
                reservation_id=_pricing_resp_get(reserve_resp, "reservation_id"),
                quote_id=_pricing_resp_get(reserve_resp, "quote_id") or pricing.get("quote_id"),
                reserved_units=_pricing_resp_get(reserve_resp, "reserved_units") or pricing.get("estimated_units"),
                amount=_pricing_resp_get(reserve_resp, "amount"),
                currency=_pricing_resp_get(reserve_resp, "currency"),
                billing_mode=_pricing_resp_get(reserve_resp, "billing_mode"),
                billing_account_id=_pricing_resp_get(reserve_resp, "billing_account_id"),
                settlement_mode=_pricing_resp_get(reserve_resp, "settlement_mode"),
                pricing_mode=_pricing_resp_get(reserve_resp, "pricing_mode"),
                entitlement_source=_pricing_resp_get(reserve_resp, "entitlement_source"),
                entitlement_reason=_pricing_resp_get(reserve_resp, "entitlement_reason"),
                tier_code=_pricing_resp_get(reserve_resp, "tier_code"),
                disabled_reason=None if reserve_status.lower() != "disabled" else _pricing_disabled_reason(),
                error=None,
            )
            logger.info(
                "commerce_pricing_reserved",
                extra={
                    "studio_job_id": str(studio_job_id),
                    "quote_id": str(req.quote_id),
                    "reservation_id": pricing.get("reservation_id"),
                    "variant_code": pricing.get("variant_code"),
                    "billing_account_id": pricing.get("billing_account_id"),
                    "settlement_mode": pricing.get("settlement_mode"),
                },
            )
        except Exception as e:  # noqa: BLE001
            code = _extract_pricing_error_code(e)
            logger.exception(
                "commerce_pricing_reserve_failed",
                extra={
                    "quote_id": str(req.quote_id),
                    "studio_job_id": str(studio_job_id),
                    "user_id": str(user_id),
                    "product_type": product_type,
                    "mode": mode,
                },
            )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "pricing_reservation_failed",
                    "code": code,
                    "message": str(e),
                },
            ) from e

    campaign_meta = {
        "source": "confirm_pricing",
        "request_hash": request_hash,
        "idempotency_key": idempotency_key,
        "quote_id": str(req.quote_id),
        "mode": mode,
        "product_type": product_type,
        "resolution": resolution,
        "pricing_state": pricing.get("state"),
        "pricing_enabled": bool(pricing.get("enabled", False)),
        "pricing_billing_mode": pricing.get("billing_mode"),
        "pricing_settlement_mode": pricing.get("settlement_mode"),
        "pricing_billing_account_id": pricing.get("billing_account_id"),
        "pricing": pricing,
    }

    resolved_final = _resolve_vton_inputs_from_request_json(request_json, storage=storage)
    payload = {
        "quote_id": str(req.quote_id),
        "input": {"quote_id": str(req.quote_id)},
        "campaign_id": str(campaign_id),
        "commerce_campaign_id": str(campaign_id),
        "quote_request": request_json,
        "request": request_json,
        "request_json": request_json,
        "resolved": resolved_final.get("resolved_json"),
        "pricing": pricing,
        "pricing_state": pricing.get("state"),
        "computed": {
            "stage": "queued",
            "request_hash": request_hash,
            "quote_id": str(req.quote_id),
            "pricing": pricing,
            "pricing_state": pricing.get("state"),
        },
        "meta": {
            "pricing": pricing,
        },
    }
    meta = {
        "request_type": "commerce_confirm",
        "request_hash": request_hash,
        "idempotency_key": idempotency_key,
        "campaign_id": str(campaign_id),
        "commerce_campaign_id": str(campaign_id),
        "quote_id": str(req.quote_id),
        "mode": mode,
        "product_type": product_type,
        "resolution": resolution,
        "confirm_pricing": True,
        "pricing": pricing,
        "pricing_state": pricing.get("state"),
        "pricing_enabled": bool(pricing.get("enabled", False)),
        "pricing_billing_mode": pricing.get("billing_mode"),
        "pricing_settlement_mode": pricing.get("settlement_mode"),
        "pricing_billing_account_id": pricing.get("billing_account_id"),
        "totals": {"usd": total_usd, "inr": total_inr},
        "total_credits": total_credits,
    }
    computed_initial = {
        "stage": "queued",
        "request_hash": request_hash,
        "quote_id": str(req.quote_id),
        "pricing": pricing,
        "pricing_state": pricing.get("state"),
    }

    try:
        async with pool.acquire() as con:
            async with con.transaction():
                await con.execute(
                    """
                    update public.commerce_quotes
                    set request_json = $3::jsonb,
                        mode = $4,
                        resolution = $5,
                        status = 'confirmed',
                        resolved_json = $6::jsonb,
                        dominant_component_code = $7,
                        resolved_garment_image_url = $8,
                        resolved_human_image_url = $9,
                        updated_at = now()
                    where id = $1 and user_id = $2
                    """,
                    req.quote_id,
                    user_id,
                    json.dumps(request_json),
                    mode,
                    resolution,
                    json.dumps(resolved_final.get("resolved_json") or {}),
                    resolved_final.get("dominant_component_code"),
                    resolved_final.get("resolved_garment_image_url"),
                    resolved_final.get("resolved_human_image_url"),
                )

                if existing_campaign:
                    await con.execute(
                        """
                        update public.commerce_campaigns
                        set mode = $3,
                            product_type = $4,
                            status = 'queued',
                            quote_id = $5,
                            input_json = $6::jsonb,
                            meta_json = coalesce(meta_json, '{}'::jsonb) || $7::jsonb,
                            updated_at = now()
                        where id = $1 and user_id = $2
                        """,
                        campaign_id,
                        user_id,
                        mode,
                        product_type,
                        req.quote_id,
                        json.dumps(request_json),
                        json.dumps(campaign_meta),
                    )
                else:
                    await con.execute(
                        """
                        insert into public.commerce_campaigns(
                            id, user_id, mode, product_type, status, quote_id, input_json, meta_json, created_at, updated_at
                        )
                        values(
                            $1, $2, $3, $4, 'queued', $5, $6::jsonb, $7::jsonb, now(), now()
                        )
                        """,
                        campaign_id,
                        user_id,
                        mode,
                        product_type,
                        req.quote_id,
                        json.dumps(request_json),
                        json.dumps(campaign_meta),
                    )

                await con.execute(
                    """
                    insert into public.studio_jobs(
                        id, studio_type, status, request_hash, payload_json, meta_json, computed_json, user_id, created_at, updated_at, next_run_at
                    )
                    values(
                        $1, 'commerce', 'queued', $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, now(), now(), now()
                    )
                    """,
                    studio_job_id,
                    request_hash,
                    json.dumps(payload),
                    json.dumps(meta),
                    json.dumps(computed_initial),
                    user_id,
                )

                await con.execute(
                    """
                    update public.commerce_campaigns
                    set meta_json = coalesce(meta_json,'{}'::jsonb) || $2::jsonb,
                        status = 'queued',
                        updated_at = now()
                    where id = $1
                    """,
                    campaign_id,
                    json.dumps(
                        {
                            "studio_job_id": str(studio_job_id),
                            "quote_id": str(req.quote_id),
                            "mode": mode,
                            "resolution": resolution,
                            "confirm_pricing": True,
                            "request_hash": request_hash,
                            "pricing_state": pricing.get("state"),
                            "pricing": pricing,
                        }
                    ),
                )

                persisted = await con.fetchrow(
                    """
                    select payload_json, meta_json, computed_json
                    from public.studio_jobs
                    where id = $1 and user_id = $2 and studio_type = 'commerce'
                    """,
                    studio_job_id,
                    user_id,
                )
                if not persisted:
                    raise RuntimeError("studio_job_insert_missing_after_confirm")

                p_payload = _as_dict(persisted["payload_json"])
                p_meta = _as_dict(persisted["meta_json"])
                p_computed = _as_dict(persisted["computed_json"])
                persisted_pricing = _pricing_from_payload_meta_computed(p_payload, p_meta, p_computed)

                if _pricing_enabled(pricing_client):
                    reservation_id = str(persisted_pricing.get("reservation_id") or "").strip()
                    if not reservation_id:
                        raise RuntimeError("pricing_persistence_failed_missing_reservation_id")

                logger.info(
                    "commerce_confirm_persisted_pricing",
                    extra={
                        "studio_job_id": str(studio_job_id),
                        "quote_id": str(req.quote_id),
                        "reservation_id": persisted_pricing.get("reservation_id"),
                        "pricing_state": persisted_pricing.get("state"),
                    },
                )

    except Exception as e:  # noqa: BLE001
        logger.exception(
            "commerce_confirm_db_write_failed",
            extra={"studio_job_id": str(studio_job_id), "campaign_id": str(campaign_id), "quote_id": str(req.quote_id)},
        )
        pricing = await _release_pricing_best_effort(
            pricing_client=pricing_client,
            user_id=user_id,
            studio_job_id=studio_job_id,
            pricing=pricing,
            reason="confirm_db_write_failed",
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "commerce_confirm_failed",
                "message": str(e),
                "pricing_state": pricing.get("state"),
            },
        ) from e

    return CommerceConfirmOut(
        campaign_id=campaign_id,
        studio_job_id=studio_job_id,
        status="queued",
    )


@router.get("/jobs/{studio_job_id}/status", operation_id="commerce_job_status_get")
async def job_status(
    studio_job_id: UUID,
    user_id: UUID = Depends(require_user),
    include_payload: int = Query(0, ge=0, le=1),
) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        j = await con.fetchrow(
            """
            select
              id, status, error_code, error_message,
              payload_json, meta_json, computed_json,
              updated_at, created_at
            from public.studio_jobs
            where id = $1 and user_id = $2 and studio_type = 'commerce'
            """,
            studio_job_id,
            user_id,
        )
    if not j:
        raise HTTPException(status_code=404, detail="job_not_found")

    payload = _as_dict(j["payload_json"])
    meta = _as_dict(j["meta_json"])

    payload_computed = _as_dict(payload.get("computed"))
    computed_col = _as_dict(j.get("computed_json"))
    computed = dict(payload_computed)
    computed.update(computed_col)

    stage = str(computed.get("stage") or j["status"] or "").strip() or str(j["status"])

    urls = computed.get("urls")
    if not isinstance(urls, list):
        urls = []
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    urls = [u for u in urls if u.lower().startswith(("http://", "https://"))]
    computed["urls"] = urls

    pricing = _pricing_from_payload_meta_computed(payload, meta, computed)
    if not pricing and computed.get("pricing_state"):
        pricing = {"state": computed.get("pricing_state")}

    out: Dict[str, Any] = {
        "studio_job_id": str(j["id"]),
        "job_id": str(j["id"]),
        "studio_type": "commerce",
        "status": j["status"],
        "stage": stage,
        "campaign_id": payload.get("campaign_id") or meta.get("campaign_id"),
        "quote_id": payload.get("quote_id") or meta.get("quote_id"),
        "created_at": j["created_at"].isoformat() if j["created_at"] else None,
        "updated_at": j["updated_at"].isoformat() if j["updated_at"] else None,
        "error_code": j["error_code"],
        "error_message": j["error_message"],
        "computed": computed,
        "urls": urls,
        "variant_count": int(computed.get("variant_count")) if isinstance(computed.get("variant_count"), int) else len(urls),
        "preview_url": urls[0] if urls else None,
        "pricing": pricing,
    }

    if include_payload:
        payload2 = dict(payload)
        payload2["computed"] = computed
        if pricing:
            payload2["pricing"] = pricing
        out["payload_json"] = payload2
        meta2 = dict(meta)
        if pricing and "pricing" not in meta2:
            meta2["pricing"] = pricing
        out["meta_json"] = meta2
        out["computed_json"] = computed_col

    return out


@router.get("/gallery", operation_id="commerce_gallery_list")
async def gallery(
    user_id: UUID = Depends(require_user),
    limit: int = Query(24, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="Opaque cursor from previous response"),
    before: Optional[str] = Query(None, description="(legacy) ISO timestamp; items strictly earlier than this"),
    only_succeeded: bool = Query(True),
) -> Dict[str, Any]:
    before_ts: Optional[datetime] = None
    before_id: Optional[str] = None

    if cursor:
        cur = _b64url_decode_json(cursor)
        before_ts_raw = cur.get("created_at")
        before_id = cur.get("id")
        if isinstance(before_ts_raw, str) and before_ts_raw.strip():
            try:
                before_ts = datetime.fromisoformat(before_ts_raw.replace("Z", "+00:00"))
            except Exception:
                before_ts = None
    elif isinstance(before, str) and before.strip():
        b = before.strip().replace("Z", "+00:00")
        try:
            before_ts = datetime.fromisoformat(b)
        except Exception:
            raise HTTPException(status_code=422, detail="invalid_before_timestamp (use ISO format)")

    pool = await get_pool()
    async with pool.acquire() as con:
        if before_ts and before_id:
            rows = await con.fetch(
                """
                select
                  id,
                  status,
                  created_at,
                  updated_at,
                  payload_json->>'quote_id' as quote_id,
                  payload_json->>'campaign_id' as campaign_id,
                  coalesce(computed_json, payload_json->'computed') as computed,
                  payload_json->'pricing' as payload_pricing,
                  meta_json->'pricing' as meta_pricing
                from public.studio_jobs
                where user_id = $1
                  and studio_type = 'commerce'
                  and ($2::bool is false or status = 'succeeded')
                  and (created_at, id) < ($3::timestamptz, $4::uuid)
                order by created_at desc, id desc
                limit $5
                """,
                user_id,
                only_succeeded,
                before_ts,
                UUID(str(before_id)),
                limit,
            )
        elif before_ts:
            rows = await con.fetch(
                """
                select
                  id,
                  status,
                  created_at,
                  updated_at,
                  payload_json->>'quote_id' as quote_id,
                  payload_json->>'campaign_id' as campaign_id,
                  coalesce(computed_json, payload_json->'computed') as computed,
                  payload_json->'pricing' as payload_pricing,
                  meta_json->'pricing' as meta_pricing
                from public.studio_jobs
                where user_id = $1
                  and studio_type = 'commerce'
                  and ($2::bool is false or status = 'succeeded')
                  and created_at < $3::timestamptz
                order by created_at desc, id desc
                limit $4
                """,
                user_id,
                only_succeeded,
                before_ts,
                limit,
            )
        else:
            rows = await con.fetch(
                """
                select
                  id,
                  status,
                  created_at,
                  updated_at,
                  payload_json->>'quote_id' as quote_id,
                  payload_json->>'campaign_id' as campaign_id,
                  coalesce(computed_json, payload_json->'computed') as computed,
                  payload_json->'pricing' as payload_pricing,
                  meta_json->'pricing' as meta_pricing
                from public.studio_jobs
                where user_id = $1
                  and studio_type = 'commerce'
                  and ($2::bool is false or status = 'succeeded')
                order by created_at desc, id desc
                limit $3
                """,
                user_id,
                only_succeeded,
                limit,
            )

    items: List[Dict[str, Any]] = []
    next_cursor: Optional[str] = None
    next_before: Optional[str] = None

    for r in rows or []:
        computed = _as_dict(r["computed"])
        urls = _normalize_urls(computed.get("urls"))
        provider_meta = _as_dict(computed.get("provider_meta"))
        resolved_inputs = _as_dict(provider_meta.get("resolved_inputs"))
        pricing = _as_dict(r["payload_pricing"]) or _as_dict(r["meta_pricing"]) or _as_dict(computed.get("pricing"))

        items.append(
            {
                "studio_job_id": str(r["id"]),
                "status": r["status"],
                "stage": computed.get("stage") or r["status"],
                "campaign_id": r["campaign_id"],
                "quote_id": r["quote_id"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "provider": computed.get("provider"),
                "variant_count": computed.get("variant_count") if isinstance(computed.get("variant_count"), int) else len(urls),
                "urls": urls,
                "preview_url": urls[0] if urls else None,
                "resolved_inputs": resolved_inputs,
                "pricing": pricing,
            }
        )

    if rows:
        last = rows[-1]
        if last["created_at"] and last["id"]:
            next_before = last["created_at"].isoformat()
            next_cursor = _b64url_encode_json({"created_at": next_before, "id": str(last["id"])})

    return {"items": items, "next_cursor": next_cursor, "next_before": next_before, "count": len(items)}


@router.get("/campaigns/{campaign_id}", operation_id="commerce_campaign_detail_get")
async def campaign_detail(campaign_id: UUID, user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        camp = await con.fetchrow(
            """
            select id, user_id, status, mode, product_type, quote_id, input_json, meta_json, created_at, updated_at
            from public.commerce_campaigns
            where id=$1 and user_id=$2
            """,
            campaign_id,
            user_id,
        )
        if not camp:
            raise HTTPException(status_code=404, detail="campaign_not_found")

        job = await con.fetchrow(
            """
            select id, status, payload_json, meta_json, computed_json, created_at, updated_at, error_code, error_message
            from public.studio_jobs
            where user_id=$1
              and studio_type='commerce'
              and (payload_json->>'campaign_id')=$2
            order by created_at desc
            limit 1
            """,
            user_id,
            str(campaign_id),
        )

    job_out: Optional[Dict[str, Any]] = None
    if job:
        payload = _as_dict(job["payload_json"])
        meta = _as_dict(job["meta_json"])
        payload_computed = _as_dict(payload.get("computed"))
        computed_col = _as_dict(job["computed_json"])
        computed = _merge_computed(payload_computed, computed_col)
        urls = _normalize_urls(computed.get("urls"))
        pricing = _pricing_from_payload_meta_computed(payload, meta, computed)

        job_out = _normalize_job_status_payload(
            {
                "studio_job_id": str(job["id"]),
                "job_id": str(job["id"]),
                "status": job["status"],
                "stage": str(computed.get("stage") or job["status"]),
                "created_at": job["created_at"].isoformat() if job["created_at"] else None,
                "updated_at": job["updated_at"].isoformat() if job["updated_at"] else None,
                "error_code": job["error_code"],
                "error_message": job["error_message"],
                "computed": computed,
                "urls": urls,
                "variant_count": computed.get("variant_count") if isinstance(computed.get("variant_count"), int) else len(urls),
                "pricing": pricing,
            }
        )

    return {
        "campaign": {
            "campaign_id": str(camp["id"]),
            "status": camp["status"],
            "mode": camp["mode"],
            "product_type": camp["product_type"],
            "quote_id": str(camp["quote_id"]) if camp["quote_id"] else None,
            "created_at": camp["created_at"].isoformat() if camp["created_at"] else None,
            "updated_at": camp["updated_at"].isoformat() if camp["updated_at"] else None,
            "meta": dict(camp["meta_json"] or {}),
        },
        "latest_job": job_out,
    }