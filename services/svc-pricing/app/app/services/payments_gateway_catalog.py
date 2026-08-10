from __future__ import annotations

import json
from typing import Any, Dict, Optional


def _as_dict_deep_loose(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (list, tuple)):
        merged: Dict[str, Any] = {}
        for item in x:
            item_dict = _as_dict_deep_loose(item)
            if item_dict:
                merged.update(item_dict)
        return merged
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            decoded = json.loads(s)
        except Exception:
            return {}
        return _as_dict_deep_loose(decoded)
    try:
        return dict(x)
    except Exception:
        return {}


async def load_apple_subscription_product_map(
    conn,
    *,
    country_code: Optional[str],
    currency: str,
) -> Dict[str, str]:
    ccy = str(currency or "USD").strip().upper() or "USD"
    cc = str(country_code or "").strip().upper()

    try:
        rows = await conn.fetch(
            """
            with ranked as (
              select
                lower(trim(internal_plan_code)) as plan_code,
                trim(apple_product_id) as apple_product_id,
                row_number() over (
                  partition by lower(trim(internal_plan_code))
                  order by
                    case
                      when $2 <> '' and upper(coalesce(country_code, '')) = $2 then 0
                      when coalesce(country_code, '') = '' then 1
                      else 2
                    end,
                    case
                      when upper(coalesce(currency, '')) = $1 then 0
                      when coalesce(currency, '') = '' then 1
                      else 2
                    end,
                    updated_at desc nulls last,
                    created_at desc nulls last
                ) as rn
              from public.apple_iap_product_mappings
              where is_active = true
                and product_type = 'subscription'
                and coalesce(trim(internal_plan_code), '') <> ''
                and coalesce(trim(apple_product_id), '') <> ''
                and (upper(coalesce(currency, '')) = $1 or coalesce(currency, '') = '')
                and (upper(coalesce(country_code, '')) = $2 or coalesce(country_code, '') = '')
            )
            select plan_code, apple_product_id
            from ranked
            where rn = 1
            """,
            ccy,
            cc,
        )
    except Exception:
        return {}

    out: Dict[str, str] = {}
    for row in rows:
        plan_code = str(row["plan_code"] or "").strip().lower()
        apple_product_id = str(row["apple_product_id"] or "").strip()
        if plan_code and apple_product_id and plan_code not in out:
            out[plan_code] = apple_product_id
    return out


async def load_apple_topup_product_map(
    conn,
    *,
    country_code: Optional[str],
    currency: str,
) -> Dict[str, str]:
    ccy = str(currency or "USD").strip().upper() or "USD"
    cc = str(country_code or "").strip().upper()

    try:
        rows = await conn.fetch(
            """
            with ranked as (
              select
                upper(trim(internal_pack_code)) as pack_code,
                trim(apple_product_id) as apple_product_id,
                row_number() over (
                  partition by upper(trim(internal_pack_code))
                  order by
                    case
                      when $2 <> '' and upper(coalesce(country_code, '')) = $2 then 0
                      when coalesce(country_code, '') = '' then 1
                      else 2
                    end,
                    case
                      when upper(coalesce(currency, '')) = $1 then 0
                      when coalesce(currency, '') = '' then 1
                      else 2
                    end,
                    updated_at desc nulls last,
                    created_at desc nulls last
                ) as rn
              from public.apple_iap_product_mappings
              where is_active = true
                and product_type = 'consumable'
                and coalesce(trim(internal_pack_code), '') <> ''
                and coalesce(trim(apple_product_id), '') <> ''
                and (upper(coalesce(currency, '')) = $1 or coalesce(currency, '') = '')
                and (upper(coalesce(country_code, '')) = $2 or coalesce(country_code, '') = '')
            )
            select pack_code, apple_product_id
            from ranked
            where rn = 1
            """,
            ccy,
            cc,
        )
    except Exception:
        return {}

    out: Dict[str, str] = {}
    for row in rows:
        pack_code = str(row["pack_code"] or "").strip().upper()
        apple_product_id = str(row["apple_product_id"] or "").strip()
        if pack_code and apple_product_id and pack_code not in out:
            out[pack_code] = apple_product_id
    return out


def enrich_plan_catalog_item_for_gateways(
    item: Dict[str, Any],
    *,
    apple_product_by_plan: Dict[str, str],
) -> Dict[str, Any]:
    out = dict(item or {})
    plan_code = str(out.get("plan_code") or "").strip().lower()
    metadata = _as_dict_deep_loose(out.get("metadata") or out.get("metadata_json"))

    stripe_price_id = str(out.get("stripe_price_id") or metadata.get("stripe_price_id") or "").strip() or None
    apple_product_id = (
        str(out.get("apple_product_id") or out.get("ios_product_id") or metadata.get("apple_product_id") or metadata.get("ios_product_id") or "").strip()
        or apple_product_by_plan.get(plan_code)
        or None
    )

    out["stripe_price_id"] = stripe_price_id
    out["apple_product_id"] = apple_product_id
    out["ios_product_id"] = apple_product_id

    if stripe_price_id:
        metadata["stripe_price_id"] = stripe_price_id
    if apple_product_id:
        metadata["apple_product_id"] = apple_product_id
        metadata["ios_product_id"] = apple_product_id

    out["metadata"] = metadata
    return out


def enrich_topup_catalog_item_for_gateways(
    item: Dict[str, Any],
    *,
    apple_product_by_pack: Dict[str, str],
) -> Dict[str, Any]:
    out = dict(item or {})
    pack_code = str(out.get("pack_code") or "").strip().upper()
    metadata = _as_dict_deep_loose(out.get("metadata") or out.get("metadata_json"))

    apple_product_id = (
        str(out.get("apple_product_id") or out.get("ios_product_id") or metadata.get("apple_product_id") or metadata.get("ios_product_id") or "").strip()
        or apple_product_by_pack.get(pack_code)
        or None
    )

    out["apple_product_id"] = apple_product_id
    out["ios_product_id"] = apple_product_id

    if apple_product_id:
        metadata["apple_product_id"] = apple_product_id
        metadata["ios_product_id"] = apple_product_id

    out["metadata"] = metadata
    return out
