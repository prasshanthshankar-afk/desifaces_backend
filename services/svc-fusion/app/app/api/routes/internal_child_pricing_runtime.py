from __future__ import annotations

from typing import Any, Dict

from app.api.routes import fusion_jobs as _fusion_jobs


_ORIGINAL_EXTRACT_PRICING_VIEW = _fusion_jobs._extract_pricing_view
_ORIGINAL_EXTRACT_PRICING_SUMMARY_VIEW = _fusion_jobs._extract_pricing_summary_view


def _internal_child_source(job: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return the persisted request shape when this job is an internal child.

    FusionJobCreate intentionally preserves arbitrary provider_options/tags while
    dropping unknown top-level compatibility fields. V3 Director duplicates the
    bill-to-parent markers into those preserved dicts, so persisted jobs retain
    enough lineage to reconstruct the canonical suppressed pricing contract.
    """
    payload = _fusion_jobs._coerce_dict(job.get("payload_json"))
    meta = _fusion_jobs._coerce_dict(job.get("meta_json"))

    for candidate in (payload, meta):
        if candidate and _fusion_jobs._is_internal_child_pricing_suppressed(candidate):
            return candidate
    return None


def _extract_pricing_view(job: Dict[str, Any]) -> dict | None:
    source = _internal_child_source(job)
    if source is not None:
        # Internal-child responses must never expose a stale/reserved/billable
        # persisted pricing object. The parent scene owns the only user charge.
        return _fusion_jobs._suppressed_pricing_payload(source)
    return _ORIGINAL_EXTRACT_PRICING_VIEW(job)


def _extract_pricing_summary_view(job: Dict[str, Any]) -> dict | None:
    source = _internal_child_source(job)
    if source is not None:
        pricing = _fusion_jobs._suppressed_pricing_payload(source)
        return _fusion_jobs._pricing_suppressed_summary(pricing)
    return _ORIGINAL_EXTRACT_PRICING_SUMMARY_VIEW(job)


def install_internal_child_pricing_runtime() -> None:
    """Install canonical internal-child pricing views for create/status APIs."""
    _fusion_jobs._extract_pricing_view = _extract_pricing_view
    _fusion_jobs._extract_pricing_summary_view = _extract_pricing_summary_view


__all__ = ["install_internal_child_pricing_runtime"]
