from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class _PricingBaseModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class PricingReserveRequest(_PricingBaseModel):
    user_id: str
    service_name: str
    service_action: str
    sku_code: str
    units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReserveResponse(_PricingBaseModel):
    status: str = "reserved"
    reservation_id: Optional[str] = None
    quote_id: Optional[str] = None
    reserved_units: Optional[str] = None
    billed_units: Optional[str] = None
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