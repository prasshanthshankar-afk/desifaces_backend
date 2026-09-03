from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.domain.models import FusionJobCreate
from app.services.fusion_orchestrator import FusionOrchestrator, PricingClientError


class FusionPricingConfirmation(BaseModel):
    """Exact user-confirmed pricing preview carried into Fusion reserve."""

    quote_id: str = Field(min_length=1, max_length=300)
    preview_fingerprint: str | None = Field(default=None, max_length=500)
    user_confirmed: bool = True

    @model_validator(mode="after")
    def require_confirmation(self):
        if self.user_confirmed is not True:
            raise ValueError("fusion_pricing_user_confirmation_required")
        return self


class ConfirmedFusionJobCreate(FusionJobCreate):
    """Additive V3 request contract; all existing Fusion fields stay unchanged."""

    pricing_confirmation: FusionPricingConfirmation | None = None


class _ConfirmedPricingClientProxy:
    """Inject the already-confirmed quote into the existing reserve call only."""

    def __init__(self, delegate: Any, *, quote_id: str, preview_fingerprint: str | None) -> None:
        self._delegate = delegate
        self._quote_id = quote_id
        self._preview_fingerprint = preview_fingerprint

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def reserve(self, req: Any):
        updates = {
            "quote_id": self._quote_id,
            "preview_fingerprint": self._preview_fingerprint,
        }
        if hasattr(req, "model_copy"):
            req = req.model_copy(update=updates)
        else:
            for key, value in updates.items():
                try:
                    setattr(req, key, value)
                except Exception:
                    pass
        return await self._delegate.reserve(req)


class ConfirmedPricingFusionOrchestrator(FusionOrchestrator):
    """Preserve current Fusion pricing math while honoring the confirmed preview.

    The existing orchestrator remains authoritative for variant selection, units,
    billing-account resolution, reserve/commit/release, provider execution and
    recovery. This bridge only carries quote_id/preview_fingerprint from the
    already-working preview endpoint into its reserve request.
    """

    def _build_initial_pricing_block(self, req: FusionJobCreate) -> dict[str, Any]:
        pricing = super()._build_initial_pricing_block(req)
        confirmation = getattr(req, "pricing_confirmation", None)
        if confirmation is not None:
            if getattr(confirmation, "user_confirmed", True) is not True:
                raise PricingClientError("fusion_pricing_user_confirmation_required")
            pricing["quote_id"] = str(getattr(confirmation, "quote_id", "") or "").strip() or None
            pricing["preview_fingerprint"] = (
                str(getattr(confirmation, "preview_fingerprint", "") or "").strip() or None
            )
        return pricing

    async def _reserve_pricing_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: dict[str, Any],
    ) -> dict[str, Any]:
        quote_id = str(pricing.get("quote_id") or "").strip()
        preview_fingerprint = str(pricing.get("preview_fingerprint") or "").strip() or None
        if not quote_id:
            return await super()._reserve_pricing_for_job(
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
            )

        original = self.pricing_client
        self.pricing_client = _ConfirmedPricingClientProxy(
            original,
            quote_id=quote_id,
            preview_fingerprint=preview_fingerprint,
        )
        try:
            return await super()._reserve_pricing_for_job(
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
            )
        finally:
            self.pricing_client = original


__all__ = [
    "ConfirmedFusionJobCreate",
    "ConfirmedPricingFusionOrchestrator",
    "FusionPricingConfirmation",
]
