# services/svc-commerce/app/app/services/commerce_processor.py
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.db import get_pool
from app.services.providers.vton_provider import (
    VTONGenerateRequest,
    VTONProvider,
    VTONVariantSpec,
)

logger = logging.getLogger(__name__)


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
      {"input": {...}}  (sometimes used as request shape)
    """
    if not d:
        return {}
    if isinstance(d.get("quote_request"), dict):
        return _as_dict(d.get("quote_request"))
    if isinstance(d.get("request"), dict):
        return _as_dict(d.get("request"))
    return d


def _extract_quote_request_anywhere(*, payload: Dict[str, Any], meta: Dict[str, Any], campaign_meta: Dict[str, Any]) -> Dict[str, Any]:
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


async def _read_quote_request_from_db(con, *, quote_id: UUID) -> Dict[str, Any]:
    """
    Pull original request_json from public.commerce_quotes (and also apply resolved_* columns if present).
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
        logger.warning("commerce_processor: could not read public.commerce_quotes for quote_id=%s err=%s", quote_id, e)
        return {}

    if not row:
        return {}

    j = _as_dict(row.get("j"))

    # 1) best candidate = request_json
    base: Dict[str, Any] = {}
    for k in ("request_json", "request", "quote_request", "input_json", "payload_json", "meta_json", "quote_json", "input"):
        d = _as_dict(j.get(k))
        d = _unwrap_request_dict(d)
        if d:
            base = d
            break

    if not base:
        base = {}

    # 2) apply resolved columns as fallback (these exist in your schema)
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
        json.dumps(payload or {}),
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
    """
    Best-effort writeback of resolved inputs to public.commerce_quotes.
    Never fail the job if this update fails.
    """
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
            json.dumps(resolved_json or {}, default=str),
            mode,
            resolution,
            dominant_component_code,
            garment_url,
            human_url,
        )
    except Exception as e:
        logger.warning("commerce_processor: failed to persist resolved quote fields quote_id=%s err=%s", quote_id, e)


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


def _extract_vton_request_parts(
    *, quote_request: Dict[str, Any], payload: Dict[str, Any], quote_id: UUID
) -> Tuple[Dict[str, Any], Dict[str, Any], str, str, int, str, Dict[str, Any]]:
    p = _as_dict(payload)
    inp = _as_dict(p.get("input"))
    qr = _as_dict(quote_request)
    qr = _unwrap_request_dict(qr)

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

    # normalize common legacy keys into the dicts
    for k in ("garment_image_url", "saree_image_url", "blouse_image_url", "primary_image_url", "product_image_url", "product_type", "cloth_type", "items"):
        if k in qr and k not in product_assets:
            product_assets[k] = qr.get(k)
        if k in inp and k not in product_assets:
            product_assets[k] = inp.get(k)

    for k in ("human_image_url", "image_url", "url", "ref_url", "photo_url", "platform_model_id", "asset_id"):
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
        "garment_image_url": product_assets.get("garment_image_url") or product_assets.get("product_image_url") or product_assets.get("primary_image_url"),
        "cloth_type": product_assets.get("cloth_type"),
        "product_type": product_assets.get("product_type") or qr.get("product_type"),
        "has_items": bool(_as_list(product_assets.get("items"))),
        "dominant_component_code": product_assets.get("dominant_component_code"),
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
        await _set_job_computed(con, job_id=job_id, stage="running", patch={"started_at": started_at, "processor": "vton_v1"})

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
                "processor": "vton_v1",
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
            json.dumps(merged_meta),
        )

    assert campaign_id is not None

    try:
        # Prefer payload/meta/campaign meta; fallback to commerce_quotes.request_json
        quote_request = _extract_quote_request_anywhere(payload=payload, meta=meta, campaign_meta=campaign_meta)
        if not quote_request:
            async with pool.acquire() as con:
                quote_request = await _read_quote_request_from_db(con, quote_id=quote_id)

        product_assets, model_ref, language, resolution, count, request_hash, debug_inputs = _extract_vton_request_parts(
            quote_request=quote_request, payload=payload, quote_id=quote_id
        )

        # Resolve dominant garment from items[] (best-effort)
        async with pool.acquire() as con:
            product_assets, dominant_code = await _apply_items_resolver_best_effort(con, product_assets=product_assets)

        product_assets = _ensure_garment_image_url(product_assets)
        model_ref = _ensure_human_image_url(model_ref)

        garment_url = product_assets.get("garment_image_url")
        human_url = model_ref.get("human_image_url")

        provider = VTONProvider()

        # ✅ CRITICAL FIX:
        # Only enforce missing inputs when REAL provider is truly required.
        must_have_inputs = bool(provider.enable_real and provider.provider == "fal" and not provider.demo_mode)

        if must_have_inputs:
            if not (isinstance(garment_url, str) and garment_url.strip()):
                raise RuntimeError("commerce_processor: missing garment_image_url (provide product_assets.items[] or garment_image_url)")
            if not (isinstance(human_url, str) and human_url.strip()):
                raise RuntimeError("commerce_processor: missing human_image_url (provide model_ref.image_url or model_ref.human_image_url)")
        else:
            # Placeholder/demo-friendly: continue even if inputs missing.
            if not (isinstance(garment_url, str) and garment_url.strip()):
                logger.warning("commerce_processor: garment_image_url missing; will proceed (placeholder/demo mode). quote_id=%s", quote_id)
                garment_url = None
            if not (isinstance(human_url, str) and human_url.strip()):
                logger.warning("commerce_processor: human_image_url missing; will proceed (placeholder/demo mode). quote_id=%s", quote_id)
                human_url = None

        dominant_component_code = product_assets.get("dominant_component_code") or dominant_code
        mode = str(_as_dict(quote_request).get("mode") or "platform_models").strip() or "platform_models"

        resolved_json = {
            "source": "commerce_processor",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "resolution": resolution,
            "dominant_component_code": dominant_component_code,
            "resolved_garment_image_url": garment_url,
            "resolved_human_image_url": human_url,
            "product_assets": product_assets,
            "model_ref": model_ref,
        }

        # Persist resolved to commerce_quotes (best-effort, allows NULL)
        async with pool.acquire() as con:
            await _persist_quote_resolved_best_effort(
                con,
                quote_id=quote_id,
                mode=mode,
                resolution=resolution,
                dominant_component_code=dominant_component_code,
                garment_url=garment_url,
                human_url=human_url,
                resolved_json=resolved_json,
            )

        debug_inputs = dict(debug_inputs or {})
        debug_inputs.update(
            {
                "garment_image_url": garment_url,
                "human_image_url": human_url,
                "dominant_component_code": dominant_component_code,
                "provider_enable_real": provider.enable_real,
                "provider_name": provider.provider,
                "provider_demo_mode": getattr(provider, "demo_mode", False),
            }
        )

        async with pool.acquire() as con:
            await _set_job_computed(
                con,
                job_id=job_id,
                stage="running",
                patch={"request_hash": request_hash, "debug_inputs": debug_inputs},
            )

        variants = _build_variants(quote_request=quote_request, request_hash=request_hash, count=count)

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

        # provider.generate will:
        # - return placeholders if enable_real is false
        # - use demo URLs if enable_real true + demo_mode true
        result = await provider.generate(req)

        urls = [u for u in (result.urls or []) if isinstance(u, str) and u.strip()]
        if not urls:
            raise RuntimeError(f"commerce_processor: VTONProvider returned no urls (provider={result.provider})")

        finished_at = datetime.now(timezone.utc).isoformat()

        async with pool.acquire() as con:
            await _set_job_computed(
                con,
                job_id=job_id,
                stage="succeeded",
                patch={
                    "finished_at": finished_at,
                    "variant_count": len(urls),
                    "urls": urls,
                    "provider": result.provider,
                    "provider_meta": _minify_provider_meta(_as_dict(result.meta)),
                    "commerce_campaign_id": str(campaign_id),
                    "quote_id": str(quote_id),
                    "request_hash": request_hash,
                },
            )

            merged_meta2 = _merge(
                merged_meta,
                {"finished_at": finished_at, "status": "succeeded", "provider": result.provider, "request_hash": request_hash},
            )
            await con.execute(
                """
                update public.commerce_campaigns
                set status='succeeded', meta_json=$2::jsonb, updated_at=now()
                where id=$1
                """,
                campaign_id,
                json.dumps(merged_meta2),
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
                patch={"failed_at": failed_at, "error": err[:2000], "commerce_campaign_id": str(campaign_id), "quote_id": str(quote_id)},
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
                    json.dumps(merged_meta_fail),
                )
            except Exception:
                pass
        raise