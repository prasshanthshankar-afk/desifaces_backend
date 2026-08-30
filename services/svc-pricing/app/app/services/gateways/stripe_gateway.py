from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Mapping, Optional

import httpx

from app.config import settings


class StripeGatewayError(RuntimeError):
    pass


class StripeSignatureError(StripeGatewayError):
    pass


class StripeGateway:
    base_url = "https://api.stripe.com"

    def __init__(self) -> None:
        self.secret_key = (getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()
        self.webhook_secret = (getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or "").strip()
        self.api_version = (getattr(settings, "STRIPE_API_VERSION", "2025-03-31.basil") or "2025-03-31.basil").strip()
        self.timeout_s = 30.0

    def is_enabled(self) -> bool:
        return bool(getattr(settings, "DF_PAYMENT_GATEWAY_ENABLED", False) and self.secret_key)

    def _auth_headers(self, *, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        if not self.secret_key:
            raise StripeGatewayError("stripe_secret_key_missing")
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Stripe-Version": self.api_version,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    @staticmethod
    def _flatten_form(prefix: str, value: Any) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if value is None:
            return out
        if isinstance(value, Mapping):
            for k, v in value.items():
                key = f"{prefix}[{k}]" if prefix else str(k)
                out.update(StripeGateway._flatten_form(key, v))
            return out
        if isinstance(value, (list, tuple)):
            for idx, item in enumerate(value):
                key = f"{prefix}[{idx}]"
                out.update(StripeGateway._flatten_form(key, item))
            return out
        if isinstance(value, bool):
            out[prefix] = "true" if value else "false"
            return out
        out[prefix] = str(value)
        return out

    async def _request(
        self,
        method: str,
        path: str,
        *,
        form: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = self._auth_headers(idempotency_key=idempotency_key)
        data = None
        if form is not None:
            flattened: Dict[str, str] = {}
            for key, value in form.items():
                flattened.update(self._flatten_form(key, value))
            data = flattened
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                data=data,
                params=params,
            )

        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": resp.text}

        if resp.status_code >= 400:
            raise StripeGatewayError(f"stripe_http_{resp.status_code}:{json.dumps(payload, default=str)}")
        return payload

    async def create_customer(self, *, email: Optional[str], metadata: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        form: Dict[str, Any] = {"metadata": metadata}
        if email:
            form["email"] = email
        return await self._request("POST", "/v1/customers", form=form, idempotency_key=idempotency_key)

    async def retrieve_customer(self, customer_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/v1/customers/{customer_id}")

    async def list_payment_methods(self, *, customer_id: str, method_type: str = "card") -> Dict[str, Any]:
        return await self._request("GET", "/v1/payment_methods", params={"customer": customer_id, "type": method_type})

    async def create_wallet_topup_checkout_session(
        self,
        *,
        customer_id: str,
        amount_minor: int,
        currency: str,
        success_url: str,
        cancel_url: str,
        wallet_order_id: str,
        user_id: str,
        credits_to_grant: str,
        idempotency_key: str,
        price_id: Optional[str] = None,
        pack_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        currency_l = currency.lower()

        metadata: Dict[str, Any] = {
            "df_order_type": "wallet_topup",
            "df_wallet_order_id": wallet_order_id,
            "df_user_id": user_id,
            "df_credits_to_grant": credits_to_grant,
            "df_currency": currency_l,
            "df_amount_minor": str(amount_minor),
            "df_service": "svc-pricing",
        }
        if pack_code:
            metadata["df_pack_code"] = str(pack_code)
        if price_id:
            metadata["df_stripe_price_id"] = str(price_id)

        if price_id:
            line_item: Dict[str, Any] = {
                "price": str(price_id),
                "quantity": 1,
            }
        else:
            # Backward-compatible fallback. Production route code should resolve
            # configured top-up packs and pass price_id so Stripe Checkout uses
            # canonical catalog prices instead of creating ad-hoc inline prices.
            line_item = {
                "price_data": {
                    "currency": currency_l,
                    "unit_amount": amount_minor,
                    "product_data": {"name": f"DesiFaces Wallet Top-up ({credits_to_grant} credits)"},
                },
                "quantity": 1,
            }

        form = {
            "mode": "payment",
            "customer": customer_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": wallet_order_id,
            "payment_method_types": ["card"],
            "billing_address_collection": "auto",
            "allow_promotion_codes": True,
            "line_items": [line_item],
            "metadata": metadata,
        }
        return await self._request("POST", "/v1/checkout/sessions", form=form, idempotency_key=idempotency_key)

    async def create_subscription_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        plan_code: str,
        purpose: str,
        currency: str,
        success_url: str,
        cancel_url: str,
        user_id: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if not price_id:
            raise StripeGatewayError("stripe_price_id_missing")
        form = {
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": user_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "payment_method_types": ["card"],
            "billing_address_collection": "auto",
            "allow_promotion_codes": True,
            "line_items": [{"price": price_id, "quantity": 1}],
            "metadata": {
                "df_order_type": purpose,
                "df_plan_code": plan_code,
                "df_currency": currency,
                "df_user_id": user_id,
                "df_service": "svc-pricing",
            },
            "subscription_data": {
                "metadata": {
                    "df_order_type": purpose,
                    "df_plan_code": plan_code,
                    "df_currency": currency,
                    "df_user_id": user_id,
                    "df_service": "svc-pricing",
                }
            },
        }
        return await self._request("POST", "/v1/checkout/sessions", form=form, idempotency_key=idempotency_key)

    async def create_billing_portal_session(self, *, customer_id: str, return_url: str) -> Dict[str, Any]:
        return await self._request("POST", "/v1/billing_portal/sessions", form={"customer": customer_id, "return_url": return_url})

    async def list_subscriptions(
        self,
        *,
        customer_id: str,
        status: str = "all",
        limit: int = 100,
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/subscriptions",
            params={"customer": customer_id, "status": status, "limit": limit},
        )

    async def retrieve_subscription(self, subscription_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/v1/subscriptions/{subscription_id}",
            params={
                "expand[]": [
                    "items.data.price",
                    "latest_invoice",
                    "customer",
                    "default_payment_method",
                ]
            },
        )

    async def update_subscription(
        self,
        *,
        subscription_id: str,
        form: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._request("POST", f"/v1/subscriptions/{subscription_id}", form=form, idempotency_key=idempotency_key)

    async def change_subscription_price(
        self,
        *,
        subscription_id: str,
        subscription_item_id: str,
        new_price_id: str,
        proration_behavior: str = "always_invoice",
        payment_behavior: str = "pending_if_incomplete",
        idempotency_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        form: Dict[str, Any] = {
            "items": [{"id": subscription_item_id, "price": new_price_id}],
            "proration_behavior": proration_behavior,
            "payment_behavior": payment_behavior,
        }
        if metadata:
            form["metadata"] = metadata
        return await self.update_subscription(subscription_id=subscription_id, form=form, idempotency_key=idempotency_key)

    async def set_cancel_at_period_end(
        self,
        *,
        subscription_id: str,
        cancel_at_period_end: bool,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.update_subscription(
            subscription_id=subscription_id,
            form={"cancel_at_period_end": cancel_at_period_end},
            idempotency_key=idempotency_key,
        )

    async def reactivate_subscription(
        self,
        *,
        subscription_id: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.set_cancel_at_period_end(
            subscription_id=subscription_id,
            cancel_at_period_end=False,
            idempotency_key=idempotency_key,
        )

    async def cancel_subscription_at_period_end(
        self,
        *,
        subscription_id: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.set_cancel_at_period_end(
            subscription_id=subscription_id,
            cancel_at_period_end=True,
            idempotency_key=idempotency_key,
        )

    def verify_webhook_signature(self, *, raw_body: bytes, stripe_signature: str) -> Dict[str, Any]:
        if not self.webhook_secret:
            raise StripeSignatureError("stripe_webhook_secret_missing")
        if not stripe_signature:
            raise StripeSignatureError("missing_stripe_signature")

        items = [item.strip() for item in stripe_signature.split(",") if item.strip()]
        timestamp: Optional[int] = None
        signatures: list[str] = []
        for item in items:
            if item.startswith("t="):
                try:
                    timestamp = int(item[2:])
                except Exception as exc:
                    raise StripeSignatureError("invalid_signature_timestamp") from exc
            elif item.startswith("v1="):
                signatures.append(item[3:])

        if timestamp is None or not signatures:
            raise StripeSignatureError("invalid_stripe_signature_header")

        tolerance = int(getattr(settings, "DF_PAYMENT_WEBHOOK_TOLERANCE_SECONDS", 300) or 300)
        now_ts = int(time.time())
        if abs(now_ts - timestamp) > tolerance:
            raise StripeSignatureError("stripe_signature_timestamp_out_of_tolerance")

        signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(self.webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, sig) for sig in signatures):
            raise StripeSignatureError("stripe_signature_mismatch")

        try:
            return json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise StripeSignatureError("invalid_webhook_json") from exc
