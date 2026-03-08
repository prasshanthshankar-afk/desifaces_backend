# services/svc-face/app/app/services/face_pricing.py
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Dict, List


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []


def _to_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def _first_non_empty(*vals: Any) -> Any:
    for v in vals:
        if v not in (None, "", [], {}, ()):
            return v
    return None


def _has_image_reference(payload: Dict[str, Any]) -> bool:
    direct_keys = [
        "reference_image_url",
        "input_image_url",
        "source_image_url",
        "image_url",
        "edit_image_url",
        "seed_artifact_url",
        "seed_artifact_id",
        "source_artifact_id",
        "reference_artifact_id",
        "face_image_url",
    ]
    for key in direct_keys:
        if payload.get(key):
            return True

    for group_key in ("input_assets", "assets", "references"):
        for item in _as_list(payload.get(group_key)):
            d = _as_dict(item)
            if (
                d.get("url")
                or d.get("image_url")
                or d.get("artifact_id")
                or d.get("asset_id")
            ):
                return True

    return False


def resolve_face_pricing(payload: Dict[str, Any]) -> Dict[str, Any]:
    requested_variants = max(
        1,
        _to_int(payload.get("num_variants"), 0),
        _to_int(payload.get("variant_count"), 0),
        _to_int(payload.get("num_outputs"), 0),
        len(_as_list(payload.get("preferred_variations"))),
    )

    is_i2i = _has_image_reference(payload)
    mode = "i2i" if is_i2i else "t2i"

    sku_code = (
        os.getenv("DF_PRICING_SKU_FACE_I2I", "face.creator.generate.i2i")
        if is_i2i
        else os.getenv("DF_PRICING_SKU_FACE_T2I", "face.creator.generate.t2i")
    )

    return {
        "service_action": f"face.creator.generate.{mode}",
        "sku_code": sku_code,
        "estimated_units": str(Decimal(requested_variants)),
        "unit_type": "image",
        "meta": {
            "mode": mode,
            "requested_variants": requested_variants,
            "has_reference_image": is_i2i,
            "requested_aspect_ratio": _first_non_empty(
                payload.get("aspect_ratio"),
                payload.get("target_aspect_ratio"),
            ),
            "provider_hint": payload.get("provider"),
        },
    }


def resolve_face_actual_units(result_payload: Dict[str, Any]) -> str:
    variants = _as_list(result_payload.get("variants"))
    if variants:
        return str(Decimal(len(variants)))

    urls = set()

    for key in ("image_url", "output_url", "artifact_url", "final_url"):
        v = result_payload.get(key)
        if isinstance(v, str) and v:
            urls.add(v)

    for list_key in ("artifacts", "outputs", "images"):
        for item in _as_list(result_payload.get(list_key)):
            d = _as_dict(item)
            for key in ("url", "image_url", "artifact_url", "output_url"):
                v = d.get(key)
                if isinstance(v, str) and v:
                    urls.add(v)

    return str(Decimal(max(1, len(urls))))