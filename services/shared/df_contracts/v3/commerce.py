"""Canonical desifaces-v3 pricing, entitlement, and credit contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from .common import V3ContractModel


class EntitlementState(StrEnum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"
    SUSPENDED = "suspended"


class CreditEntryType(StrEnum):
    SUBSCRIPTION_GRANT = "subscription_grant"
    TOP_UP = "top_up"
    CONSUMPTION = "consumption"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    EXPIRY = "expiry"


class CreditReservationState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


class PriceMoney(V3ContractModel):
    currency: str = Field(min_length=3, max_length=3)
    amount_minor: int = Field(ge=0)


class PricingQuote(V3ContractModel):
    quote_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    user_id: UUID
    operation: str = Field(min_length=1, max_length=100)
    credits: int = Field(ge=0)
    money: PriceMoney | None = None
    pricebook_revision: str = Field(min_length=1, max_length=100)
    fingerprint: str = Field(min_length=8, max_length=500)
    expires_at: datetime
    created_at: datetime


class CreditReservation(V3ContractModel):
    reservation_id: UUID
    account_id: UUID
    user_id: UUID
    quote_id: UUID
    state: CreditReservationState
    reserved_credits: int = Field(ge=0)
    reference_type: str | None = Field(default=None, max_length=100)
    reference_id: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Entitlement(V3ContractModel):
    entitlement_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    plan_code: str = Field(min_length=1, max_length=100)
    state: EntitlementState
    provider: str | None = Field(default=None, max_length=100)
    provider_subscription_id: str | None = Field(default=None, max_length=500)
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CreditTransaction(V3ContractModel):
    transaction_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    user_id: UUID | None = None
    entry_type: CreditEntryType
    credits_delta: int
    balance_after: int = Field(ge=0)
    reference_type: str | None = Field(default=None, max_length=100)
    reference_id: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)
    created_at: datetime
