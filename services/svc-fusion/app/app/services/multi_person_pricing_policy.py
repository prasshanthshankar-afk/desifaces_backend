from __future__ import annotations

from typing import Any, Dict

from desifaces_shared.pricing.multi_person import participant_count, select_multi_person_pricing


def _context_from_request(req: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    context: Dict[str, Any] = dict(payload or {})
    tags = getattr(req, "tags", None)
    if isinstance(tags, dict):
        context["tags"] = dict(tags)
    for name in ("preview_metadata", "generation_metadata"):
        value = getattr(req, name, None)
        if isinstance(value, dict):
            context[name] = dict(value)
    return context


def install_multi_person_pricing_policy() -> None:
    """Install a narrow, idempotent Fusion pricing selector."""
    from app.services.fusion_orchestrator import FusionOrchestrator

    original = FusionOrchestrator._build_initial_pricing_block
    if getattr(original, "_desifaces_multi_person_pricing", False):
        return

    def wrapped(self: FusionOrchestrator, req: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        pricing = dict(original(self, req, payload) or {})
        context = _context_from_request(req, payload)
        count = participant_count(context)
        if count < 2:
            return pricing

        units = pricing.get("estimated_units") or pricing.get("requested_units") or 1
        selection = select_multi_person_pricing(
            studio="fusion",
            participant_count_value=count,
            natural_units=units,
        )
        if selection is None:
            return pricing

        metadata = dict(pricing.get("metadata") or pricing.get("meta") or {})
        metadata.update(selection.metadata)
        pricing.update(
            {
                "sku_code": selection.sku_code,
                "variant_code": selection.variant_code,
                "estimated_units": str(selection.natural_units),
                "variant_params": selection.variant_params,
                "metadata": metadata,
                "meta": metadata,
                "multi_person": True,
                "premium": True,
                "participant_count": selection.participant_count,
                "pricing_policy": selection.metadata["pricing_policy"],
            }
        )
        return pricing

    wrapped._desifaces_multi_person_pricing = True  # type: ignore[attr-defined]
    FusionOrchestrator._build_initial_pricing_block = wrapped
