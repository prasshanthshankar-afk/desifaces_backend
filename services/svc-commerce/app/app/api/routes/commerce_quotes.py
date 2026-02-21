from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import require_user
from app.db import get_pool
from app.domain.models import CommerceConfirmIn, CommerceConfirmOut, CommerceQuoteIn, CommerceQuoteOut
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
    """
    Some clients send:
      { "request": {...} }  or  { "quote_request": {...} }  or  { "input": {...} }

    For /quote we treat inner dict as the "quote request" and merge into the outer body.
    Outer keys are preserved unless overridden by inner.
    """
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
    """
    For /confirm we allow patching the stored request_json using either:
      - nested: { quote_request: {...} } / { request: {...} } / { input: {...} }
      - OR top-level fields: { product_assets: {...}, model_ref: {...}, resolution: ..., ... }

    We never merge quote_id/idempotency_key into request_json.
    """
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
    }
    patch: Dict[str, Any] = {}
    for k in allowed:
        if k in raw:
            patch[k] = raw.get(k)
    return patch


def _deep_merge_request_json(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge patch over base with shallow-merge for known nested dicts.
    """
    out = dict(base or {})
    patch = dict(patch or {})

    for k in ("product_assets", "model_ref", "outputs", "views", "cta"):
        if isinstance(out.get(k), dict) and isinstance(patch.get(k), dict):
            merged = dict(out[k])
            merged.update(patch[k])
            patch[k] = merged

    out.update(patch)
    return out


def _resolve_vton_inputs_from_request_json(request_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resolves and normalizes:
      - product_assets.garment_image_url (from items[] or legacy keys)
      - product_assets.dominant_component_code (best guess)
      - model_ref.human_image_url
    Also sets lightweight garment-type hints for saree/dress cases (provider may use these).
    """
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

    # patch canonical keys
    if garment_url:
        pa["garment_image_url"] = garment_url
    if dominant_code:
        pa["dominant_component_code"] = dominant_code
    if _is_http_url(human_url):
        mr["human_image_url"] = str(human_url).strip()

    # lightweight garment-type hints (helps providers pick "dresses" for saree/lehenga)
    # Do not override user-provided values.
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


def _require_vton_inputs_or_422(*, request_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fail fast for FE + demo reliability.
    Enforced in /confirm so worker does not fail later.
    """
    resolved = _resolve_vton_inputs_from_request_json(request_json)
    garment_url = resolved.get("resolved_garment_image_url")
    human_url = resolved.get("resolved_human_image_url")

    missing: List[str] = []
    if not _is_http_url(garment_url):
        missing.append("product_assets.items[].image_url OR product_assets.garment_image_url")
    if not _is_http_url(human_url):
        missing.append("model_ref.human_image_url OR model_ref.image_url")

    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "missing_vton_inputs",
                "missing": missing,
                "hint": "Send garment + human URLs in /quote or /confirm. Wrapper bodies {request:{...}} and {quote_request:{...}} are supported.",
            },
        )
    return resolved


# ---------------------------
# API
# ---------------------------

@router.post("/quote", response_model=CommerceQuoteOut)
async def quote(req: CommerceQuoteIn, request: Request, user_id: UUID = Depends(require_user)) -> CommerceQuoteOut:
    """
    Supports both canonical body and wrapper bodies:
      - canonical: {mode, product_type, resolution, product_assets, model_ref, outputs...}
      - wrappers:  {mode,..., request:{...}} OR {quote_request:{...}} OR {input:{...}}
    """
    raw: Dict[str, Any] = {}
    try:
        raw_j = await request.json()
        raw = _as_dict(raw_j)
    except Exception:
        raw = {}

    normalized = _extract_wrapped_request(raw) if raw else req.model_dump(mode="json")

    # Re-validate using normalized body so wrapper clients work reliably.
    try:
        req2 = CommerceQuoteIn.model_validate(normalized)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"invalid_quote_request: {type(e).__name__}: {e}") from e

    pc = PricingClient()
    out = await pc.quote(user_id=user_id, req=req2)

    pool = await get_pool()
    req_json = req2.model_dump(mode="json")
    out_json = out.model_dump(mode="json")

    # Resolve best-effort (do NOT 422 here; quoting may happen before images are selected)
    resolved = _resolve_vton_inputs_from_request_json(req_json)

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


@router.post("/confirm", response_model=CommerceConfirmOut)
async def confirm(req: CommerceConfirmIn, request: Request, user_id: UUID = Depends(require_user)) -> CommerceConfirmOut:
    pool = await get_pool()
    now = datetime.now(timezone.utc)

    # Accept wrapper bodies here too (so FE/E2E can pass images in confirm even if quote was earlier)
    raw: Dict[str, Any] = {}
    try:
        raw_j = await request.json()
        raw = _as_dict(raw_j)
    except Exception:
        raw = {}

    idem_from_raw = raw.get("idempotency_key") if isinstance(raw, dict) else None
    idempotency_key = req.idempotency_key or (str(idem_from_raw).strip() if isinstance(idem_from_raw, str) and idem_from_raw.strip() else None)

    async with pool.acquire() as con:
        async with con.transaction():
            user_exists = await con.fetchval("select 1 from core.users where id = $1", user_id)
            if not user_exists:
                raise HTTPException(
                    status_code=401,
                    detail="unknown_user_in_core_users (token sub not present in core.users; use a token issued by svc-core login/signup)",
                )

            # lock quote row during confirm for consistency
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
                raise HTTPException(status_code=404, detail="Quote not found")

            if q["expires_at"] <= now:
                raise HTTPException(status_code=422, detail="Quote expired")

            if q["status"] not in ("quoted", "confirmed"):
                raise HTTPException(status_code=422, detail=f"Quote not confirmable: {q['status']}")

            request_json: Dict[str, Any] = dict(q["request_json"] or {})
            request_json.setdefault("product_assets", {})
            request_json.setdefault("model_ref", {})

            # Merge patch from client (nested or top-level) into stored quote request_json
            patch = _extract_confirm_patch(raw)
            if patch:
                request_json = _deep_merge_request_json(request_json, patch)

            # ensure mode/resolution present
            if not request_json.get("mode") and q.get("mode"):
                request_json["mode"] = q["mode"]
            if not request_json.get("resolution") and q.get("resolution"):
                request_json["resolution"] = q["resolution"]

            # ensure resolved urls present from quote columns
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

            # HARD FAIL-FAST HERE (prevents worker failures + saves demo)
            resolved = _require_vton_inputs_or_422(request_json=request_json)

            request_json_for_job = dict(request_json)
            request_json_for_job["product_assets"] = resolved.get("product_assets") or request_json.get("product_assets")
            request_json_for_job["model_ref"] = resolved.get("model_ref") or request_json.get("model_ref")

            mode = str(request_json_for_job.get("mode") or "platform_models").strip() or "platform_models"
            product_type = str(request_json_for_job.get("product_type") or "mixed").strip() or "mixed"
            resolution = str(request_json_for_job.get("resolution") or (q.get("resolution") or "hd")).strip() or "hd"

            # persist latest request_json + resolved to commerce_quotes
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

            # campaign create or reuse
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
                # include both keys so commerce_processor can find it regardless of extraction strategy
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

            # mark quote confirmed
            await con.execute(
                "update public.commerce_quotes set status='confirmed', updated_at=now() where id=$1 and user_id=$2",
                req.quote_id,
                user_id,
            )

            # help the worker + FE find the campaign/job quickly
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
                    }
                ),
            )

            return CommerceConfirmOut(campaign_id=campaign_id, studio_job_id=studio_job_id, status="queued")


@router.get("/jobs/{studio_job_id}/status")
async def job_status(
    studio_job_id: UUID,
    user_id: UUID = Depends(require_user),
    include_payload: int = Query(0, ge=0, le=1),
) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        j = await con.fetchrow(
            """
            select id, status, error_code, error_message, payload_json, meta_json, updated_at, created_at
            from public.studio_jobs
            where id = $1 and user_id = $2 and studio_type = 'commerce'
            """,
            studio_job_id,
            user_id,
        )
    if not j:
        raise HTTPException(status_code=404, detail="job_not_found")

    payload = _as_dict(j["payload_json"])
    computed = _as_dict(payload.get("computed"))
    stage = str(computed.get("stage") or j["status"] or "").strip() or str(j["status"])

    urls = computed.get("urls")
    if not isinstance(urls, list):
        urls = []
    urls = [u for u in urls if isinstance(u, str) and u.strip()]

    out: Dict[str, Any] = {
        "studio_job_id": str(j["id"]),
        "studio_type": "commerce",
        "status": j["status"],
        "stage": stage,
        "campaign_id": payload.get("campaign_id"),
        "quote_id": payload.get("quote_id"),
        "created_at": j["created_at"].isoformat() if j["created_at"] else None,
        "updated_at": j["updated_at"].isoformat() if j["updated_at"] else None,
        "error_code": j["error_code"],
        "error_message": j["error_message"],
        "computed": computed,
        "urls": urls,
        "preview_url": urls[0] if urls else None,
    }

    if include_payload:
        out["payload_json"] = payload
        out["meta_json"] = _as_dict(j["meta_json"])

    return out


# ---------------------------
# PRODUCTION SCALE DEMO ENDPOINTS
# ---------------------------

@router.get("/gallery")
async def gallery(
    user_id: UUID = Depends(require_user),
    limit: int = Query(24, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="Opaque cursor from previous response"),
    before: Optional[str] = Query(None, description="(legacy) ISO timestamp; items strictly earlier than this"),
    only_succeeded: bool = Query(True),
) -> Dict[str, Any]:
    """
    Production-scale list endpoint for FE demo:
      - Keyset pagination (created_at,id) via `cursor`
      - Backward-compatible `before` timestamp pagination
      - Returns compact items with urls + resolved inputs (if available)
    """
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
                  payload_json->'computed' as computed
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
                  payload_json->'computed' as computed
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
                  payload_json->'computed' as computed
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
        urls = computed.get("urls") if isinstance(computed.get("urls"), list) else []
        urls = [u for u in urls if isinstance(u, str) and u.strip()]

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
                "variant_count": computed.get("variant_count"),
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


@router.get("/campaigns/{campaign_id}")
async def campaign_detail(campaign_id: UUID, user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    """
    Production endpoint: one campaign + latest job + urls for FE.
    """
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
            select id, status, payload_json, created_at, updated_at, error_code, error_message
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
        computed = _as_dict(payload.get("computed"))
        urls = computed.get("urls") if isinstance(computed.get("urls"), list) else []
        urls = [u for u in urls if isinstance(u, str) and u.strip()]

        job_out = {
            "studio_job_id": str(job["id"]),
            "status": job["status"],
            "stage": str(computed.get("stage") or job["status"]),
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
            "updated_at": job["updated_at"].isoformat() if job["updated_at"] else None,
            "error_code": job["error_code"],
            "error_message": job["error_message"],
            "computed": computed,
            "urls": urls,
            "preview_url": urls[0] if urls else None,
        }

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