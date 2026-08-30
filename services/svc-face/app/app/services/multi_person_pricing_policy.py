from __future__ import annotations

from typing import Any, Dict

from desifaces_shared.pricing.multi_person import (
    face_units,
    participant_count,
    select_multi_person_pricing,
)


def install_multi_person_pricing_policy() -> None:
    """Install a narrow, idempotent Face Creator pricing selector."""
    from app.services.creator_orchestrator import CreatorOrchestrator

    original = CreatorOrchestrator._build_initial_pricing_block
    if getattr(original, "_desifaces_multi_person_pricing", False):
        return

    def wrapped(self: CreatorOrchestrator, request_dict: Dict[str, Any]) -> Dict[str, Any]:
        pricing = dict(original(self, request_dict) or {})
        count = participant_count(request_dict)
        if count < 2:
            return pricing

        units = face_units(
            pricing.get("estimated_units")
            or request_dict.get("num_variants")
            or request_dict.get("variant_count")
            or 1
        )
        selection = select_multi_person_pricing(
            studio="face",
            participant_count_value=count,
            natural_units=units,
        )
        if selection is None:
            return pricing

        # svc-pricing expands pricing.meta into variant params. Keep the existing
        # Face metadata and add the exact quantity key expected by FACE_MULTI_PERSON.
        meta = dict(pricing.get("meta") or {})
        meta.update(selection.metadata)
        meta.update(selection.variant_params)
        pricing.update(
            {
                "sku_code": selection.sku_code,
                "variant_code": selection.variant_code,
                "estimated_units": str(selection.natural_units),
                "variant_params": selection.variant_params,
                "meta": meta,
                "metadata": dict(meta),
                "multi_person": True,
                "premium": True,
                "participant_count": selection.participant_count,
                "pricing_policy": selection.metadata["pricing_policy"],
            }
        )
        return pricing

    wrapped._desifaces_multi_person_pricing = True  # type: ignore[attr-defined]
    CreatorOrchestrator._build_initial_pricing_block = wrapped
