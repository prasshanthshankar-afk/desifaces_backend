from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Path

from pydantic import BaseModel, Field

from app.api.deps import require_user
from app.db import get_pool

router = APIRouter(prefix="/api/commerce", tags=["commerce"])


class ProductAssetIn(BaseModel):
    role: str = Field(..., description="Asset role, stored into commerce_product_assets.asset_type")
    asset_id: UUID = Field(..., description="media_assets.id")
    optional: bool = False


class MerchantProductCreateIn(BaseModel):
    category: str = "apparel"
    title: str
    sku: Optional[str] = None

    outfit_kind: str = Field("saree_set", description="e.g. saree_set")
    default_drape_style: str = "nivi"
    metadata: Dict[str, Any] = Field(default_factory=dict)

    assets: List[ProductAssetIn] = Field(default_factory=list)


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


@router.post("/merchants/{merchant_id}/products", operation_id="commerce_merchant_product_create")
async def create_merchant_product(
    merchant_id: UUID,
    body: MerchantProductCreateIn,
    user_id: UUID = Depends(require_user),
) -> Dict[str, Any]:
    # For now: merchant_id == authenticated user (simple + safe). Later: vendor keys can upload/manage.
    if merchant_id != user_id:
        raise HTTPException(status_code=403, detail="merchant_id_must_equal_authenticated_user_for_now")

    pool = await get_pool()
    product_id = uuid4()
    md = dict(body.metadata or {})
    md["outfit_kind"] = body.outfit_kind
    md["default_drape_style"] = body.default_drape_style

    async with pool.acquire() as con:
        await con.execute(
            """
            insert into commerce_products(id, user_id, category, title, sku, status, metadata_json, created_at, updated_at)
            values($1,$2,$3,$4,$5,'draft',$6::jsonb, now(), now())
            """,
            product_id,
            merchant_id,
            body.category,
            body.title,
            body.sku,
            json.dumps(md),
        )

        # Attach assets
        for a in body.assets:
            role = (a.role or "").strip()
            if not role:
                continue

            ma = await con.fetchrow("select id, user_id, width, height, storage_ref from media_assets where id=$1", a.asset_id)
            if not ma:
                raise HTTPException(status_code=404, detail=f"media_asset_not_found: {a.asset_id}")
            if UUID(str(ma["user_id"])) != merchant_id:
                raise HTTPException(status_code=403, detail=f"asset_not_owned_by_merchant: {a.asset_id}")

            await con.execute(
                """
                insert into commerce_product_assets(id, product_id, asset_type, media_asset_id, artifact_id, meta_json, created_at)
                values($1,$2,$3,$4,null,$5::jsonb, now())
                """,
                uuid4(),
                product_id,
                role,
                a.asset_id,
                json.dumps({"optional": bool(a.optional)}),
            )

    return {"product_id": str(product_id), "status": "draft"}


@router.get("/merchants/{merchant_id}/products", operation_id="commerce_merchant_product_list")
async def list_merchant_products(merchant_id: UUID, user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    if merchant_id != user_id:
        raise HTTPException(status_code=403, detail="merchant_id_must_equal_authenticated_user_for_now")

    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            select id, category, title, sku, status, metadata_json, created_at, updated_at
            from commerce_products
            where user_id=$1
            order by created_at desc
            limit 200
            """,
            merchant_id,
        )
    items = []
    for r in rows:
        md = _as_dict(r["metadata_json"])
        items.append(
            {
                "product_id": str(r["id"]),
                "category": r["category"],
                "title": r["title"],
                "sku": r["sku"],
                "status": r["status"],
                "outfit_kind": md.get("outfit_kind"),
                "default_drape_style": md.get("default_drape_style"),
                "metadata": md,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
        )
    return {"items": items, "count": len(items)}


@router.get("/products/{product_id}", operation_id="commerce_product_get")
async def get_product(product_id: UUID = Path(...), user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        p = await con.fetchrow(
            "select id, user_id, category, title, sku, status, metadata_json, created_at, updated_at from commerce_products where id=$1",
            product_id,
        )
        if not p:
            raise HTTPException(status_code=404, detail="product_not_found")

        # Owner-only for now
        if UUID(str(p["user_id"])) != user_id:
            raise HTTPException(status_code=403, detail="forbidden")

        assets = await con.fetch(
            """
            select id, asset_type, media_asset_id, meta_json, created_at
            from commerce_product_assets
            where product_id=$1
            order by created_at asc
            """,
            product_id,
        )

    md = _as_dict(p["metadata_json"])
    out_assets = []
    for a in assets:
        meta = _as_dict(a["meta_json"])
        out_assets.append(
            {
                "link_id": str(a["id"]),
                "role": a["asset_type"],
                "asset_id": str(a["media_asset_id"]) if a["media_asset_id"] else None,
                "optional": bool(meta.get("optional", False)),
                "meta": meta,
            }
        )

    return {
        "product_id": str(p["id"]),
        "merchant_id": str(p["user_id"]),
        "category": p["category"],
        "title": p["title"],
        "sku": p["sku"],
        "status": p["status"],
        "metadata": md,
        "assets": out_assets,
    }


@router.post("/products/{product_id}/validate", operation_id="commerce_product_validate")
async def validate_product(product_id: UUID, user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        p = await con.fetchrow("select id, user_id, category, status, metadata_json from commerce_products where id=$1", product_id)
        if not p:
            raise HTTPException(status_code=404, detail="product_not_found")
        if UUID(str(p["user_id"])) != user_id:
            raise HTTPException(status_code=403, detail="forbidden")

        md = _as_dict(p["metadata_json"])
        outfit_kind = str(md.get("outfit_kind") or "saree_set").strip() or "saree_set"

        # Load combos for outfit_kind (recommended/best)
        combos = await con.fetch(
            """
            select combo_code, display_name, constraints_json
            from commerce_component_combinations
            where (constraints_json->>'outfit_kind') = $1
            order by combo_code asc
            """,
            outfit_kind,
        )

        # Load product asset roles + dimensions
        rows = await con.fetch(
            """
            select cpa.asset_type as role, ma.width, ma.height, ma.content_type
            from commerce_product_assets cpa
            left join media_assets ma on ma.id = cpa.media_asset_id
            where cpa.product_id = $1
            """,
            product_id,
        )

    roles_present = {str(r["role"]).strip() for r in rows if r["role"]}
    role_meta = {str(r["role"]).strip(): r for r in rows if r["role"]}

    def _roles_from_constraints(cj: Dict[str, Any], key: str) -> List[str]:
        v = cj.get(key)
        if isinstance(v, list):
            return [str(x).strip() for x in v if isinstance(x, str) and str(x).strip()]
        return []

    chosen_combo = None
    chosen_required: List[str] = []
    chosen_optional: List[str] = []

    for c in combos:
        cj = _as_dict(c["constraints_json"])
        req_roles = _roles_from_constraints(cj, "required_asset_roles")
        if req_roles and all(r in roles_present for r in req_roles):
            chosen_combo = c
            chosen_required = req_roles
            chosen_optional = _roles_from_constraints(cj, "optional_asset_roles")
            break

    if not chosen_combo and combos:
        # fallback: first combo (usually recommended)
        chosen_combo = combos[0]
        cj = _as_dict(chosen_combo["constraints_json"])
        chosen_required = _roles_from_constraints(cj, "required_asset_roles")
        chosen_optional = _roles_from_constraints(cj, "optional_asset_roles")

    missing = [r for r in chosen_required if r not in roles_present]

    # Basic quality warnings by role
    warnings: List[Dict[str, Any]] = []
    thresholds = {
        "saree_full": (768, 1024),
        "pallu_full": (768, 1024),
        "border_closeup": (512, 512),
        "worn_ref_front": (768, 1024),
    }
    for role, (min_w, min_h) in thresholds.items():
        if role in roles_present:
            rm = role_meta.get(role) or {}
            w = rm.get("width")
            h = rm.get("height")
            if isinstance(w, int) and isinstance(h, int):
                if w < min_w or h < min_h:
                    warnings.append(
                        {
                            "code": "LOW_RES",
                            "role": role,
                            "message": f"{role} is {w}x{h}; recommend >= {min_w}x{min_h}",
                        }
                    )

    ok = len(missing) == 0
    pack_level = None
    if chosen_combo:
        cj = _as_dict(chosen_combo["constraints_json"])
        pack_level = cj.get("pack_level")

    validation = {
        "ok": ok,
        "outfit_kind": outfit_kind,
        "combo_code": chosen_combo["combo_code"] if chosen_combo else None,
        "pack_level": pack_level,
        "required_roles": chosen_required,
        "optional_roles": chosen_optional,
        "missing_roles": missing,
        "warnings": warnings,
    }

    # Persist into metadata_json.validation + status when ok
    async with pool.acquire() as con:
        await con.execute(
            """
            update commerce_products
            set metadata_json = coalesce(metadata_json,'{}'::jsonb) || jsonb_build_object('validation',$2::jsonb),
                status = case when $3::bool then 'validated' else status end,
                updated_at = now()
            where id=$1
            """,
            product_id,
            json.dumps(validation),
            ok,
        )

    return validation


@router.post("/products/{product_id}/publish", operation_id="commerce_product_publish")
async def publish_product(product_id: UUID, user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        p = await con.fetchrow("select id, user_id, metadata_json from commerce_products where id=$1", product_id)
        if not p:
            raise HTTPException(status_code=404, detail="product_not_found")
        if UUID(str(p["user_id"])) != user_id:
            raise HTTPException(status_code=403, detail="forbidden")

        md = _as_dict(p["metadata_json"])
        v = _as_dict(md.get("validation"))
        if not v.get("ok"):
            raise HTTPException(status_code=422, detail={"error": "product_not_validated", "validation": v})

        await con.execute("update commerce_products set status='active', updated_at=now() where id=$1", product_id)

    return {"product_id": str(product_id), "status": "active"}