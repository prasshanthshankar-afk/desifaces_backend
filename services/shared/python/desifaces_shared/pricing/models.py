from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _compact_dict(value: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in value.items() if v is not None}


class _PricingBaseModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class _PricingResponseModel(_PricingBaseModel):
    """
    Backward-compatible response base.

    Existing callers can keep reading legacy top-level fields, while newer
    callers/frontend can consume:
      - pricing
      - pricing_summary
    """

    pricing: Dict[str, Any] = Field(default_factory=dict)
    pricing_summary: Dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None

    def pricing_dict(self) -> Dict[str, Any]:
        pricing = dict(self.pricing or {})
        pricing.setdefault("state", _clean_text(getattr(self, "status", None)))
        pricing.setdefault("enabled", pricing.get("state") != "disabled")
        for field_name in (
            "quote_id",
            "reservation_id",
            "service_name",
            "service_action",
            "sku_code",
            "billing_mode",
            "pricing_mode",
            "entitlement_source",
            "entitlement_reason",
            "tier_code",
            "billing_account_id",
            "settlement_mode",
            "currency",
            "ledger_entry_id",
            "message",
        ):
            value = _clean_text(getattr(self, field_name, None))
            if value is not None and field_name not in pricing:
                pricing[field_name] = value

        scalar_mappings = {
            "estimated_units": getattr(self, "estimated_units", None),
            "reserved_units": getattr(self, "reserved_units", None),
            "released_units": getattr(self, "released_units", None),
            "actual_units": getattr(self, "actual_units", None),
            "billed_units": getattr(self, "billed_units", None),
            "amount": getattr(self, "amount", None),
            "estimated_amount": getattr(self, "estimated_amount", None),
        }
        for key, value in scalar_mappings.items():
            if value is not None and key not in pricing:
                pricing[key] = str(value)

        return _compact_dict(pricing)

    def pricing_summary_dict(self) -> Dict[str, Any]:
        summary = dict(self.pricing_summary or {})
        nested_summary = getattr(self, "summary", None)
        if isinstance(nested_summary, dict):
            summary.update({k: v for k, v in nested_summary.items() if v is not None})

        if "display_estimate" not in summary:
            estimate = (
                getattr(self, "estimated_amount", None)
                or summary.get("estimated_amount")
                or summary.get("amount")
            )
            if estimate is not None:
                summary["display_estimate"] = str(estimate)

        if "display_final" not in summary:
            final_amount = getattr(self, "amount", None) or summary.get("final_amount")
            if final_amount is not None:
                summary["display_final"] = str(final_amount)

        return _compact_dict(summary)

    def model_post_init(self, __context: Any) -> None:
        if not self.pricing:
            self.pricing = self.pricing_dict()
        if not self.pricing_summary:
            self.pricing_summary = self.pricing_summary_dict()


class PricingPreviewRequest(_PricingBaseModel):
    """
    Internal service-to-service preview request used before generate.

    This is intentionally studio-agnostic:
    - user_id: real end-user id
    - service_name: caller service (svc-face, svc-audio, svc-fusion, ...)
    - service_action: action string like face.creator.generate.t2i
    - sku_code: billing sku / variant code for preview lookup
    - units: requested units as a string for consistency with reserve/commit models
    - external_ref_type/external_ref_id: optional linkage for caller traceability
    - idempotency_key: preview idempotency
    - meta: normalized studio-specific inputs and context
    """

    user_id: str
    service_name: str
    service_action: str
    sku_code: str
    units: str
    external_ref_type: str = "studio_job_preview"
    external_ref_id: Optional[str] = None
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingPreviewResponse(_PricingResponseModel):
    """
    Canonical preview response for all studios.

    Frontend can derive:
    - estimate card
    - balance before/after
    - confirmation payload
    """

    status: str = "quoted"

    quote_id: Optional[str] = None
    quote_expires_at: Optional[str] = None
    preview_fingerprint: Optional[str] = None

    service_name: Optional[str] = None
    service_action: Optional[str] = None
    sku_code: Optional[str] = None
    unit_type: Optional[str] = None

    estimated_units: Optional[str] = None
    estimated_amount: Optional[str] = None
    currency: Optional[str] = None

    billing_mode: Optional[str] = None
    pricing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    entitlement_reason: Optional[str] = None
    tier_code: Optional[str] = None

    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None

    before_credits: Optional[str] = None
    after_estimated_credits: Optional[str] = None
    before_money: Optional[str] = None
    after_estimated_money: Optional[str] = None

    quote_breakdown: Dict[str, Any] = Field(default_factory=dict)
    summary: Dict[str, Any] = Field(default_factory=dict)


class PricingReserveRequest(_PricingBaseModel):
    user_id: str
    service_name: str
    service_action: str
    sku_code: str
    units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str

    # Tie reserve to the preview the user confirmed.
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None

    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReserveResponse(_PricingResponseModel):
    status: str = "reserved"
    reservation_id: Optional[str] = None

    # Traceability back to preview
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None

    reserved_units: Optional[str] = None
    billed_units: Optional[str] = None
    actual_units: Optional[str] = None

    # "amount" remains for backward compatibility with existing callers.
    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None

    billing_mode: Optional[str] = None
    pricing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    entitlement_reason: Optional[str] = None
    tier_code: Optional[str] = None

    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None


class PricingCommitRequest(_PricingBaseModel):
    user_id: str
    reservation_id: str
    actual_units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingCommitResponse(_PricingResponseModel):
    status: str = "committed"
    reservation_id: Optional[str] = None
    actual_units: Optional[str] = None
    billed_units: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None

    billing_mode: Optional[str] = None
    pricing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    entitlement_reason: Optional[str] = None
    tier_code: Optional[str] = None

    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None


class PricingReleaseRequest(_PricingBaseModel):
    user_id: str
    reservation_id: str
    reason: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReleaseResponse(_PricingResponseModel):
    status: str = "released"
    reservation_id: Optional[str] = None
    released_units: Optional[str] = None

    billing_mode: Optional[str] = None
    pricing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    entitlement_reason: Optional[str] = None
    tier_code: Optional[str] = None

    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None
