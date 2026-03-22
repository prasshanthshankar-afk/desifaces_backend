from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class _PricingBaseModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class PricingPreviewRequest(_PricingBaseModel):
    """
    Internal service-to-service preview request used before generate.

    This is intentionally studio-agnostic:
    - user_id: real end-user id
    - service_name: caller service (svc-face, svc-audio, svc-fusion, ...)
    - service_action: action string like face.creator.generate.t2i
    - sku_code: billing sku
    - units: requested units as a string for consistency with existing reserve models
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


class PricingPreviewResponse(_PricingBaseModel):
    """
    Canonical preview response for all studios.

    The frontend can derive:
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

    message: Optional[str] = None


class PricingReserveRequest(_PricingBaseModel):
    user_id: str
    service_name: str
    service_action: str
    sku_code: str
    units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str

    # New: tie reserve to the preview the user confirmed
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None

    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReserveResponse(_PricingBaseModel):
    status: str = "reserved"
    reservation_id: Optional[str] = None

    # Traceability back to preview
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None

    reserved_units: Optional[str] = None
    billed_units: Optional[str] = None

    # "amount" remains for backward compatibility with existing callers.
    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None

    # Entitlement result
    billing_mode: Optional[str] = None
    pricing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None
    entitlement_reason: Optional[str] = None
    tier_code: Optional[str] = None

    # Billing ownership / settlement
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None

    message: Optional[str] = None


class PricingCommitRequest(_PricingBaseModel):
    user_id: str
    reservation_id: str
    actual_units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingCommitResponse(_PricingBaseModel):
    status: str = "committed"
    reservation_id: Optional[str] = None
    billed_units: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None

    # Entitlement result
    billing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None

    # Billing ownership / settlement
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None

    message: Optional[str] = None


class PricingReleaseRequest(_PricingBaseModel):
    user_id: str
    reservation_id: str
    reason: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReleaseResponse(_PricingBaseModel):
    status: str = "released"
    reservation_id: Optional[str] = None
    released_units: Optional[str] = None

    # Entitlement result
    billing_mode: Optional[str] = None
    entitlement_source: Optional[str] = None

    # Billing ownership / settlement
    billing_account_id: Optional[str] = None
    settlement_mode: Optional[str] = None

    message: Optional[str] = None