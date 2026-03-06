from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import require_user
from app.db import get_pool

router = APIRouter(prefix="/api/commerce/templates", tags=["commerce"])


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


@router.get("/components", operation_id="commerce_templates_components")
async def list_components(user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            select code, display_name, kind, is_accessory, dominance_rank
            from commerce_garment_components
            order by dominance_rank asc, code asc
            """
        )
    return {
        "items": [
            {
                "code": r["code"],
                "display_name": r["display_name"],
                "kind": r["kind"],
                "is_accessory": bool(r["is_accessory"]),
                "dominance_rank": int(r["dominance_rank"]),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/combinations", operation_id="commerce_templates_combinations")
async def list_combinations(
    outfit_kind: str = Query("saree_set"),
    user_id: UUID = Depends(require_user),
) -> Dict[str, Any]:
    outfit_kind = (outfit_kind or "").strip() or "saree_set"
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            select combo_code, display_name, primary_component_code, component_codes, constraints_json
            from commerce_component_combinations
            where (constraints_json->>'outfit_kind') = $1
            order by combo_code asc
            """,
            outfit_kind,
        )

    items: List[Dict[str, Any]] = []
    for r in rows:
        cj = _as_dict(r["constraints_json"])
        items.append(
            {
                "combo_code": r["combo_code"],
                "display_name": r["display_name"],
                "primary_component_code": r["primary_component_code"],
                "component_codes": list(r["component_codes"] or []),
                "pack_level": cj.get("pack_level"),
                "required_asset_roles": cj.get("required_asset_roles") or [],
                "optional_asset_roles": cj.get("optional_asset_roles") or [],
                "allowed_drape_styles": cj.get("allowed_drape_styles") or [],
                "notes": cj.get("notes"),
            }
        )

    return {"items": items, "count": len(items)}


@router.get("/asset_roles", operation_id="commerce_templates_asset_roles")
async def get_asset_roles(
    outfit_kind: str = Query("saree_set"),
    pack_level: str = Query("recommended"),
    user_id: UUID = Depends(require_user),
) -> Dict[str, Any]:
    outfit_kind = (outfit_kind or "").strip() or "saree_set"
    pack_level = (pack_level or "").strip() or "recommended"

    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            select constraints_json
            from commerce_component_combinations
            where (constraints_json->>'outfit_kind')=$1
              and (constraints_json->>'pack_level')=$2
            limit 1
            """,
            outfit_kind,
            pack_level,
        )

    cj = _as_dict(row["constraints_json"]) if row else {}
    return {
        "outfit_kind": outfit_kind,
        "pack_level": pack_level,
        "required_asset_roles": cj.get("required_asset_roles") or [],
        "optional_asset_roles": cj.get("optional_asset_roles") or [],
        "allowed_drape_styles": cj.get("allowed_drape_styles") or [],
        "notes": cj.get("notes"),
    }