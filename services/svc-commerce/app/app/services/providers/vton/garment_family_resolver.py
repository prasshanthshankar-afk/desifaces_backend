# services/svc-commerce/app/app/services/providers/vton/garment_family_resolver.py
from __future__ import annotations

from typing import Any, Dict, List, Optional


PHASE1_INDIAN_NON_SAREE_FAMILIES = {
    "salwar_suit",
    "lehenga_set",
    "kurta_pyjama",
    "sherwani",
}


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


def _norm(x: Any) -> str:
    return str(x or "").strip().lower()


def _blob_from_product_assets(
    *,
    product_assets: Dict[str, Any],
    ns: Dict[str, Any],
    garment_type: str,
    primary_url: Optional[str],
) -> str:
    parts: List[str] = [
        _norm(product_assets.get("garment_kind")),
        _norm(product_assets.get("outfit_kind")),
        _norm(product_assets.get("garment_type")),
        _norm(product_assets.get("dominant_component_code")),
        _norm(ns.get("dominant_component_code")),
        _norm(product_assets.get("title")),
        _norm(product_assets.get("name")),
        _norm(product_assets.get("category")),
        _norm(garment_type),
        _norm(primary_url),
    ]

    for it in _as_list(product_assets.get("items")):
        d = _as_dict(it)
        parts.extend(
            [
                _norm(d.get("component_code")),
                _norm(d.get("kind")),
                _norm(d.get("type")),
                _norm(d.get("name")),
                _norm(d.get("category")),
                _norm(d.get("image_url")),
            ]
        )

    for it in _as_list(ns.get("items_norm")):
        d = _as_dict(it)
        parts.extend(
            [
                _norm(d.get("component_code")),
                _norm(d.get("kind")),
                _norm(d.get("name")),
                _norm(d.get("category")),
                _norm(d.get("image_url")),
            ]
        )

    return " | ".join([p for p in parts if p])


def infer_indian_non_saree_family(
    *,
    product_assets: Dict[str, Any],
    ns: Dict[str, Any],
    garment_type: str,
    primary_url: Optional[str],
) -> Optional[str]:
    blob = _blob_from_product_assets(
        product_assets=product_assets,
        ns=ns,
        garment_type=garment_type,
        primary_url=primary_url,
    )

    # explicit/family-level signals first
    if any(t in blob for t in ("salwar_suit", "salwar suit", "shalwar", "salwar kameez", "kameez")):
        return "salwar_suit"

    if any(t in blob for t in ("lehenga_set", "lehenga set", "lehenga choli", "ghagra choli")):
        return "lehenga_set"

    if any(t in blob for t in ("kurta_pyjama", "kurta pyjama", "kurta pajama", "pyjama set", "pajama set")):
        return "kurta_pyjama"

    if "sherwani" in blob:
        return "sherwani"

    # item-composition fallback
    has_kurta = "kurta" in blob
    has_pyjama = ("pyjama" in blob) or ("pajama" in blob)
    if has_kurta and has_pyjama:
        return "kurta_pyjama"

    has_lehenga = "lehenga" in blob
    has_choli = "choli" in blob
    if has_lehenga and has_choli:
        return "lehenga_set"

    has_salwar = "salwar" in blob
    has_kameez = "kameez" in blob
    if has_salwar or has_kameez:
        return "salwar_suit"

    return None


def is_phase1_indian_non_saree_family(family: Optional[str]) -> bool:
    return str(family or "").strip().lower() in PHASE1_INDIAN_NON_SAREE_FAMILIES


def infer_runtime_garment_type(
    *,
    family: Optional[str],
    current_garment_type: str,
) -> str:
    fam = _norm(family)
    gt = _norm(current_garment_type)

    # For current QC/provider orchestration, these should be treated as full-look families,
    # not reduced to western upper/lower semantics.
    if fam in PHASE1_INDIAN_NON_SAREE_FAMILIES:
        return "dresses"

    if gt in {"upper_body", "lower_body", "dresses"}:
        return gt

    return "dresses"


def infer_platform_garment_kind(
    *,
    product_assets: Dict[str, Any],
    ns: Dict[str, Any],
    garment_type: str,
    primary_url: Optional[str],
) -> Optional[str]:
    family = infer_indian_non_saree_family(
        product_assets=product_assets,
        ns=ns,
        garment_type=garment_type,
        primary_url=primary_url,
    )
    if family:
        return family

    blob = _blob_from_product_assets(
        product_assets=product_assets,
        ns=ns,
        garment_type=garment_type,
        primary_url=primary_url,
    )

    # generic fallback only for non-Indian-family routing
    if any(
        t in blob
        for t in (
            "hoodie", "blazer", "jacket", "coat", "overcoat", "sweater",
            "cardigan", "shirt", "tshirt", "t-shirt", "top", "blouse"
        )
    ):
        return "upper_body"

    if any(
        t in blob
        for t in (
            "jeans", "pant", "pants", "trouser", "trousers", "skirt",
            "shorts", "pyjama", "pajama", "dhoti", "lungi"
        )
    ):
        return "lower_body"

    if any(
        t in blob
        for t in (
            "dress", "gown", "jumpsuit", "onepiece", "one-piece", "anarkali", "suit"
        )
    ):
        return "dresses"

    gt = _norm(garment_type)
    if gt in {"upper_body", "lower_body", "dresses"}:
        return gt

    return None