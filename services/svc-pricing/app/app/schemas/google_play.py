from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class GoogleSubscriptionConfirmIn(BaseModel):
    """Android client -> svc-pricing subscription confirmation.

    The mobile client should pass the Play Billing purchase token. In
    production, svc-pricing verifies the token with the Google Play Developer
    API before applying the entitlement.
    """

    model_config = ConfigDict(extra="allow")

    google_product_id: Optional[str] = None
    product_id: Optional[str] = None
    base_plan_id: Optional[str] = None
    purchase_token: str
    package_name: Optional[str] = None
    order_id: Optional[str] = None
    acknowledged: Optional[bool] = None
    currency: Optional[str] = None
    country_code: Optional[str] = None
    raw_purchase_json: Dict[str, Any] = Field(default_factory=dict)


class GoogleSubscriptionConfirmOut(BaseModel):
    ok: bool = True
    provider: str = "google_play"
    google_product_id: str
    base_plan_id: Optional[str] = None
    plan_code: str
    tier_code: str
    subscription_state: str
    entitlement_state: str
    current_period_start: Optional[str] = None
    current_period_end: Optional[str] = None
    purchase_token_hash: str
    verification_mode: str
    acknowledgement_state: Optional[str] = None


class GoogleCreditsConfirmIn(BaseModel):
    """Android client -> svc-pricing consumable credit-pack confirmation."""

    model_config = ConfigDict(extra="allow")

    google_product_id: Optional[str] = None
    product_id: Optional[str] = None
    purchase_token: str
    package_name: Optional[str] = None
    order_id: Optional[str] = None
    acknowledged: Optional[bool] = None
    consumed: Optional[bool] = None
    currency: Optional[str] = None
    country_code: Optional[str] = None
    raw_purchase_json: Dict[str, Any] = Field(default_factory=dict)


class GoogleCreditsConfirmOut(BaseModel):
    ok: bool = True
    provider: str = "google_play"
    google_product_id: str
    internal_pack_code: str
    granted_credits: int
    wallet_order_id: Optional[str] = None
    purchase_token_hash: str
    verification_mode: str
    acknowledgement_state: Optional[str] = None
    consumption_state: Optional[str] = None


class GooglePubSubMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    data: Optional[str] = None
    messageId: Optional[str] = None
    message_id: Optional[str] = None
    publishTime: Optional[str] = None
    attributes: Dict[str, Any] = Field(default_factory=dict)


class GoogleNotificationIn(BaseModel):
    """Google Real-time Developer Notification push payload.

    Supports both the standard Pub/Sub push envelope:
      {"message": {"data": "base64-json", "messageId": "..."}}

    and a decoded/direct JSON payload for local/internal tests.
    """

    model_config = ConfigDict(extra="allow")

    message: Optional[GooglePubSubMessage] = None
    subscription: Optional[str] = None

    # Direct decoded RTDN fields, useful for tests/internal replay.
    version: Optional[str] = None
    packageName: Optional[str] = None
    eventTimeMillis: Optional[str] = None
    subscriptionNotification: Optional[Dict[str, Any]] = None
    oneTimeProductNotification: Optional[Dict[str, Any]] = None
    voidedPurchaseNotification: Optional[Dict[str, Any]] = None
    testNotification: Optional[Dict[str, Any]] = None


class GoogleNotificationOut(BaseModel):
    ok: bool = True
    provider: str = "google_play"
    message_id: Optional[str] = None
    notification_type: Optional[str] = None
    google_product_id: Optional[str] = None
    purchase_token_hash: Optional[str] = None
    processing_status: str = "processed"
    verification_mode: str
