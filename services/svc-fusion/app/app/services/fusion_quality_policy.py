from __future__ import annotations

from typing import Any, Dict

from app.services.providers.omnihuman_adapter import OmniHumanAdapter

_INSTALLED = False


def _coerce_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def install_fusion_quality_policy() -> None:
    """
    Ensure OmniHuman quality is selected from the trusted server-side Pricing tier.

    Fusion persists pricing into studio_jobs.payload_json before worker execution.
    The provider adapter historically looked only at browser/provider_options tier
    hints, so paid users could fall back to the free-tier 720p path.  This policy
    injects pricing.tier_code into the adapter's internal tier lookup without
    trusting a client-supplied entitlement flag.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    original = OmniHumanAdapter._tier_code

    def wrapped(self: OmniHumanAdapter, request_payload: Dict[str, Any]) -> str:
        payload = dict(request_payload or {})
        pricing = _coerce_dict(payload.get("pricing"))
        trusted_tier = str(
            pricing.get("tier_code")
            or pricing.get("entitlement_tier_code")
            or ""
        ).strip()
        if trusted_tier:
            provider_options = _coerce_dict(payload.get("provider_options"))
            provider_options["_pricing_tier_code"] = trusted_tier
            payload["provider_options"] = provider_options
        return original(self, payload)

    OmniHumanAdapter._tier_code = wrapped  # type: ignore[assignment]
    _INSTALLED = True
