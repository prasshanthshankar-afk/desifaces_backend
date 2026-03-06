# /Users/home/Desktop/products/desifaces-backend/desifaces_backend/services/svc-commerce/app/app/api/routes/commerce_quotes.py
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import require_user
from app.db import get_pool
from app.domain.models import CommerceConfirmIn, CommerceConfirmOut, CommerceQuoteIn, CommerceQuoteOut
from app.services.azure_storage_service import AzureStorageService
from app.services.pricing_client import PricingClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/commerce", tags=["commerce"])


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
    """
    Accept list[str] or single str. Strip/validate http(s).
    """
    urls: List[str] = []
    if isinstance(x, str) and x.strip():
        urls = [x.strip()]
    elif isinstance(x, list):
        urls = [u.strip() for u in x if isinstance(u, str) and u.strip()]
    else:
        urls = []
    return [u for u in urls if u.lower().startswith(("http://", "https://"))]


def _merge_computed(payload_computed: Dict[str, Any], computed_col: Dict[str, Any]) -> Dict[str, Any]:
    """
    computed_json (column) wins over payload_json['computed'].
    """
    out = dict(payload_computed or {})
    out.update(computed_col or {})
    return out


def _normalize_job_status_payload(out: Dict[str, Any]) -> Dict[str, Any]:
    """
    Product-grade contract:
      - always include top-level urls (mirror computed.urls)
      - ensure computed.urls exists and matches urls
      - ensure preview_url and variant_count
    """
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

    for k in ("product_assets", "model_ref", "outputs", "views", "cta"):
        if isinstance(out.get(k), dict) and isinstance(patch.get(k), dict):
            merged = dict(out[k])
            merged.update(patch[k])
            patch[k] = merged

    out.update(patch)
    return out


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


def _resolve_vton_inputs_from_request_json(request_json: Dict[str, Any], *, storage: Optional[AzureStorageService] = None) -> Dict[str, Any]:
    pa = dict(_as_dict(request_json.get("product_assets")))
    mr = dict(_as_dict(request_json.get("model_ref")))

    dominant_code: Optional[str] = None
    garment_url: Optional[str] = None

    items = pa.get("items")
    if isinstance(items, list) and items:
        best: Optional[Dict[str, Any]] = None
        best_score = -10**9
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
        for k in ("garment_image_url", "product_image_url", "primary_image_url", "saree_image_url", "blouse_image_url"):
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
        if pa.get("saree_image_url") or "saree" in url_l or "lehenga" in url_l or "anarkali" in url_l or "gown" in url_l or "dress" in url_l:
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

            url = await _signed_url_for_media_asset(con=con, storage=storage, asset_id=UUID(str(ma_id)), expected_user_id=user_id)
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
    await _expand_product_ids_into_product_assets(con=con, storage=storage, request_json=request_json, user_id=user_id)
    await _resolve_model_asset_id_to_url(con=con, storage=storage, request_json=request_json, user_id=user_id)

    resolved = _resolve_vton_inputs_from_request_json(request_json, storage=storage)
    request_json["product_assets"] = resolved.get("product_assets") or request_json.get("product_assets")
    request_json["model_ref"] = resolved.get("model_ref") or request_json.get("model_ref")
    return resolved


def _require_vton_inputs_or_422(*, request_json: Dict[str, Any], storage: AzureStorageService) -> Dict[str, Any]:
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
                "hint": "Vendor flow: set mode=platform_models and provide saree URL only; backend auto-picks default model. "
                        "Customer flow: provide model_ref.human_image_url (or model_ref.asset_id). "
                        "Wrapper bodies {request:{...}} and {quote_request:{...}} are supported.",
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

    try:
        req2 = CommerceQuoteIn.model_validate(normalized)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"invalid_quote_request: {type(e).__name__}: {e}") from e

    pc = PricingClient()
    out = await pc.quote(user_id=user_id, req=req2)

    pool = await get_pool()
    req_json = req2.model_dump(mode="json")
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
    idempotency_key = req.idempotency_key or (str(idem_from_raw).strip() if isinstance(idem_from_raw, str) and idem_from_raw.strip() else None)

    storage = AzureStorageService()

    async with pool.acquire() as con:
        async with con.transaction():
            user_exists = await con.fetchval("select 1 from core.users where id = $1", user_id)
            if not user_exists:
                raise HTTPException(
                    status_code=401,
                    detail="unknown_user_in_core_users (token sub not present in core.users; use a token issued by svc-core login/signup)",
                )

            q = await con.fetchrow(
                """
                select
                  id, user_id, status, expires_at, request_json,
                  mode, resolution,
                  dominant_component_code, resolved_garment_image_url, resolved_human_image_url, resolved_json
                from public.commerce_quotes
                where id = $1 and user_id = $2
                for update
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

            request_json: Dict[str, Any] = dict(q["request_json"] or {})
            request_json.setdefault("product_assets", {})
            request_json.setdefault("model_ref", {})

            patch = _extract_confirm_patch(raw)
            if patch:
                request_json = _deep_merge_request_json(request_json, patch)

            if not request_json.get("mode") and q.get("mode"):
                request_json["mode"] = q["mode"]
            if not request_json.get("resolution") and q.get("resolution"):
                request_json["resolution"] = q["resolution"]

            pa = _as_dict(request_json.get("product_assets"))
            mr = _as_dict(request_json.get("model_ref"))
            if q.get("resolved_garment_image_url") and not _is_http_url(pa.get("garment_image_url")):
                pa["garment_image_url"] = q["resolved_garment_image_url"]
            if q.get("dominant_component_code") and not pa.get("dominant_component_code"):
                pa["dominant_component_code"] = q["dominant_component_code"]
            if q.get("resolved_human_image_url") and not _is_http_url(mr.get("human_image_url")):
                mr["human_image_url"] = q["resolved_human_image_url"]
            request_json["product_assets"] = pa
            request_json["model_ref"] = mr

            resolved = await _ensure_confirm_request_has_urls(con=con, storage=storage, request_json=request_json, user_id=user_id)
            resolved = _require_vton_inputs_or_422(request_json=request_json, storage=storage)

            request_json_for_job = dict(request_json)
            request_json_for_job["product_assets"] = resolved.get("product_assets") or request_json.get("product_assets")
            request_json_for_job["model_ref"] = resolved.get("model_ref") or request_json.get("model_ref")

            mode = str(request_json_for_job.get("mode") or "platform_models").strip() or "platform_models"
            product_type = str(request_json_for_job.get("product_type") or "mixed").strip() or "mixed"
            resolution = str(request_json_for_job.get("resolution") or (q.get("resolution") or "hd")).strip() or "hd"

            await con.execute(
                """
                update public.commerce_quotes
                set request_json = $3::jsonb,
                    resolved_json = $4::jsonb,
                    mode = $5,
                    resolution = $6,
                    dominant_component_code = $7,
                    resolved_garment_image_url = $8,
                    resolved_human_image_url = $9,
                    updated_at = now()
                where id = $1 and user_id = $2
                """,
                req.quote_id,
                user_id,
                json.dumps(request_json_for_job),
                json.dumps(resolved.get("resolved_json") or {}),
                mode,
                resolution,
                resolved.get("dominant_component_code") or q.get("dominant_component_code"),
                resolved.get("resolved_garment_image_url") or q.get("resolved_garment_image_url"),
                resolved.get("resolved_human_image_url") or q.get("resolved_human_image_url"),
            )

            existing_campaign = await con.fetchrow(
                """
                select id
                from public.commerce_campaigns
                where user_id = $1 and quote_id = $2
                order by created_at desc
                limit 1
                """,
                user_id,
                req.quote_id,
            )

            if existing_campaign:
                campaign_id = UUID(str(existing_campaign["id"]))
            else:
                campaign_id = uuid4()
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
                    json.dumps(request_json_for_job),
                    json.dumps({"source": "confirm", "idempotency_key": idempotency_key}),
                )

            idem = idempotency_key or str(req.quote_id)
            request_hash = _stable_hash({"quote_id": str(req.quote_id), "idempotency_key": idem, "kind": "commerce_confirm"})

            payload = {
                "quote_id": str(req.quote_id),
                "campaign_id": str(campaign_id),
                "quote_request": request_json_for_job,
                "request": request_json_for_job,
                "resolved": resolved.get("resolved_json") or {},
            }
            meta = {
                "request_type": "commerce_confirm",
                "idempotency_key": idempotency_key,
                "campaign_id": str(campaign_id),
                "quote_id": str(req.quote_id),
                "mode": mode,
                "product_type": product_type,
                "resolution": resolution,
            }

            row = await con.fetchrow(
                """
                insert into public.studio_jobs(
                    studio_type, status, request_hash, payload_json, meta_json, user_id, created_at, updated_at, next_run_at
                )
                values('commerce', 'queued', $1, $2::jsonb, $3::jsonb, $4, now(), now(), now())
                on conflict (user_id, studio_type, request_hash)
                do update set updated_at = now()
                returning id
                """,
                request_hash,
                json.dumps(payload),
                json.dumps(meta),
                user_id,
            )
            studio_job_id = UUID(str(row["id"]))

            await con.execute(
                "update public.commerce_quotes set status='confirmed', updated_at=now() where id=$1 and user_id=$2",
                req.quote_id,
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
                json.dumps({"studio_job_id": str(studio_job_id), "quote_id": str(req.quote_id), "mode": mode, "resolution": resolution}),
            )

            return CommerceConfirmOut(campaign_id=campaign_id, studio_job_id=studio_job_id, status="queued")


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

    # ✅ IMPORTANT: worker writes computed_json; payload_json["computed"] is not reliable.
    payload_computed = _as_dict(payload.get("computed"))
    computed_col = _as_dict(j.get("computed_json"))

    computed = dict(payload_computed)
    computed.update(computed_col)  # computed_json wins

    stage = str(computed.get("stage") or j["status"] or "").strip() or str(j["status"])

    # ✅ ALWAYS mirror computed.urls -> top-level urls (vendor/E2E contract)
    urls = computed.get("urls")
    if not isinstance(urls, list):
        urls = []
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    urls = [u for u in urls if u.lower().startswith(("http://", "https://"))]
    computed["urls"] = urls

    out: Dict[str, Any] = {
        "studio_job_id": str(j["id"]),
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
        "urls": urls,  # <-- this fixes “succeeded but urls empty”
        "variant_count": int(computed.get("variant_count")) if isinstance(computed.get("variant_count"), int) else len(urls),
        "preview_url": urls[0] if urls else None,
    }

    if include_payload:
        payload2 = dict(payload)
        payload2["computed"] = computed  # helpful for debugging
        out["payload_json"] = payload2
        out["meta_json"] = meta

    return out


# ---------------------------
# PRODUCTION SCALE DEMO ENDPOINTS
# ---------------------------

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
                  coalesce(computed_json, payload_json->'computed') as computed
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
                  coalesce(computed_json, payload_json->'computed') as computed
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
                  coalesce(computed_json, payload_json->'computed') as computed
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
            select id, status, payload_json, computed_json, created_at, updated_at, error_code, error_message
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
        payload_computed = _as_dict(payload.get("computed"))
        computed_col = _as_dict(job["computed_json"])
        computed = _merge_computed(payload_computed, computed_col)
        urls = _normalize_urls(computed.get("urls"))

        job_out = _normalize_job_status_payload(
            {
                "studio_job_id": str(job["id"]),
                "status": job["status"],
                "stage": str(computed.get("stage") or job["status"]),
                "created_at": job["created_at"].isoformat() if job["created_at"] else None,
                "updated_at": job["updated_at"].isoformat() if job["updated_at"] else None,
                "error_code": job["error_code"],
                "error_message": job["error_message"],
                "computed": computed,
                "urls": urls,
                "variant_count": computed.get("variant_count") if isinstance(computed.get("variant_count"), int) else len(urls),
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