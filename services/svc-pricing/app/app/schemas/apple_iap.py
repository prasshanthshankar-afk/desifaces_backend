from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class AppleSubscriptionConfirmIn(BaseModel):
    apple_product_id: str
    signed_transaction_info: str
    signed_renewal_info: Optional[str] = None
    transaction_id: Optional[str] = None
    original_transaction_id: Optional[str] = None
    environment: Optional[str] = None
    app_account_token: Optional[str] = None
    currency: Optional[str] = None
    country_code: Optional[str] = None
    storefront: Optional[str] = None


class AppleSubscriptionConfirmOut(BaseModel):
    ok: bool = True
    provider: str = "apple_iap"
    apple_product_id: str
    plan_code: str
    tier_code: str
    subscription_state: str
    entitlement_state: str
    current_period_start: Optional[str] = None
    current_period_end: Optional[str] = None
    verification_mode: str


class AppleCreditsConfirmIn(BaseModel):
    apple_product_id: str
    signed_transaction_info: str
    transaction_id: Optional[str] = None
    original_transaction_id: Optional[str] = None
    environment: Optional[str] = None
    app_account_token: Optional[str] = None
    currency: Optional[str] = None
    country_code: Optional[str] = None
    storefront: Optional[str] = None


class AppleCreditsConfirmOut(BaseModel):
    ok: bool = True
    provider: str = "apple_iap"
    apple_product_id: str
    internal_pack_code: str
    granted_credits: int
    wallet_order_id: Optional[str] = None
    verification_mode: str


class AppleNotificationIn(BaseModel):
    signedPayload: str


class AppleNotificationOut(BaseModel):
    ok: bool = True
    notification_uuid: Optional[str] = None
    notification_type: Optional[str] = None
    subtype: Optional[str] = None
    processing_status: str = "processed"
    verification_mode: str
