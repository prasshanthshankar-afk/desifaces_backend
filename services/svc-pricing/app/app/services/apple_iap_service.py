from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import HTTPException

from app.config import settings
from app.repo.apple_iap_repo import (
    as_dict_loose,
    apply_subscription_entitlement,
    best_effort_grant_apple_credits,
    fetch_credit_pack_profile,
    fetch_plan_profile,
    find_subscription_user_id,
    record_apple_notification_event,
    resolve_apple_product_mapping,
    revert_user_to_free_entitlement,
    upsert_apple_subscription_row,
    upsert_apple_transaction_audit,
)
from app.services.payments.wallet_fulfillment_service import fulfill_wallet_topup_order
from app.services.entitlements.plan_credit_reconciliation_service import reconcile_included_plan_credits
from app.schemas.apple_iap import (
    AppleCreditsConfirmIn,
    AppleCreditsConfirmOut,
    AppleNotificationIn,
    AppleNotificationOut,
    AppleSubscriptionConfirmIn,
    AppleSubscriptionConfirmOut,
)


def apple_iap_enabled() -> bool:
    raw = str(
        os.getenv("DF_APPLE_IAP_ENABLE")
        or getattr(settings, "DF_APPLE_IAP_ENABLE", False)
        or ""
    ).strip().lower()
    return raw in {"1", "true", "t", "yes", "y", "on"}


def apple_iap_verification_mode() -> str:
    return str(
        os.getenv("DF_APPLE_IAP_VERIFICATION_MODE")
        or getattr(settings, "DF_APPLE_IAP_VERIFICATION_MODE", "decode_only")
        or "decode_only"
    ).strip().lower()


def require_apple_iap_enabled() -> None:
    if not apple_iap_enabled():
        raise HTTPException(status_code=503, detail="apple_iap_disabled")


def apple_iap_auto_fulfill_topups_enabled() -> bool:
    raw = str(
        os.getenv("DF_APPLE_IAP_AUTO_FULFILL_TOPUPS")
        or getattr(settings, "DF_APPLE_IAP_AUTO_FULFILL_TOPUPS", False)
        or ""
    ).strip().lower()
    return raw in {"1", "true", "t", "yes", "y", "on"}


def normalize_currency(value: Optional[str]) -> str:
    return str(value or getattr(settings, "DF_WALLET_TOPUP_CURRENCY", "USD") or "USD").strip().upper()


def normalize_country_code_value(value: Optional[str]) -> str:
    return str(value or "").strip().upper()


def normalize_apple_environment(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"sandbox", "xcode", "local"}:
        return "Sandbox"
    if raw in {"production", "prod", "live"}:
        return "Production"
    return "Sandbox" if apple_iap_verification_mode() == "decode_only" else "Production"


def jws_to_payload_dict(token: Optional[str]) -> Dict[str, Any]:
    raw = str(token or "").strip()
    if not raw:
        return {}
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + padding).encode("utf-8"))
        value = json.loads(decoded.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def as_uuid_or_none(value: Any):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except Exception:
        return None


def apple_epoch_ms_to_datetime(value: Any) -> Optional[datetime]:
    if value in (None, "", 0, "0"):
        return None
    try:
        ivalue = int(str(value))
    except Exception:
        return None
    if ivalue <= 0:
        return None
    return datetime.fromtimestamp(ivalue / 1000.0, tz=timezone.utc)


def apple_cycle_key(*, interval_code: str, period_start: Optional[datetime], period_end: Optional[datetime]) -> str:
    anchor = period_start or period_end or datetime.now(timezone.utc)
    interval = str(interval_code or "monthly").strip().lower()
    if interval == "yearly":
        return f"{anchor.year:04d}"
    return f"{anchor.year:04d}-{anchor.month:02d}"


def coerce_transaction_country_code(
    explicit_country_code: Optional[str],
    auth_country_code: Optional[str],
    storefront: Optional[str],
) -> str:
    cc = normalize_country_code_value(explicit_country_code)
    if cc:
        return cc
    cc = normalize_country_code_value(auth_country_code)
    if cc:
        return cc
    storefront_code = str(storefront or "").strip().upper()
    if storefront_code == "IND":
        return "IN"
    return ""


def derive_subscription_state_from_notification(
    notification_type: str,
    subtype: Optional[str],
    decoded_txn: Dict[str, Any],
) -> Dict[str, Any]:
    ntype = str(notification_type or "").strip().upper()
    sub = str(subtype or "").strip().upper()
    if ntype in {"EXPIRED", "REVOKE", "REFUND", "REFUND_DECLINED"}:
        return {
            "subscription_state": "canceled",
            "entitlement_state": "inactive",
            "cancel_at_period_end": False,
            "revert_to_free": True,
        }
    if ntype in {"DID_FAIL_TO_RENEW", "GRACE_PERIOD_EXPIRED"}:
        return {
            "subscription_state": "past_due",
            "entitlement_state": "grace",
            "cancel_at_period_end": False,
            "revert_to_free": False,
        }
    if ntype == "DID_CHANGE_RENEWAL_STATUS" and sub == "AUTO_RENEW_DISABLED":
        return {
            "subscription_state": "active",
            "entitlement_state": "active",
            "cancel_at_period_end": True,
            "revert_to_free": False,
        }
    if ntype in {"SUBSCRIBED", "DID_RENEW", "DID_RECOVER", "RENEWAL_EXTENDED"}:
        return {
            "subscription_state": "active",
            "entitlement_state": "active",
            "cancel_at_period_end": False,
            "revert_to_free": False,
        }
    if str(decoded_txn.get("offerType") or "").strip():
        return {
            "subscription_state": "trialing",
            "entitlement_state": "active",
            "cancel_at_period_end": False,
            "revert_to_free": False,
        }
    return {
        "subscription_state": "active",
        "entitlement_state": "active",
        "cancel_at_period_end": False,
        "revert_to_free": False,
    }


def _raise_from_repo_error(exc: Exception) -> None:
    detail = str(exc)
    if detail in {
        "apple_iap_tables_missing",
        "apple_iap_mappings_missing",
    }:
        raise HTTPException(status_code=503, detail=detail)
    if detail in {
        "apple_product_not_mapped",
        "apple_plan_profile_missing",
        "apple_credit_pack_missing",
    }:
        raise HTTPException(status_code=422, detail=detail)
    raise exc


async def _find_existing_apple_wallet_order_id(
    conn,
    *,
    user_id: UUID,
    transaction_id: str,
) -> Optional[str]:
    row = await conn.fetchrow(
        '''
        select id
        from public.payment_wallet_orders
        where user_id = $1
          and idempotency_key = $2
        limit 1
        ''',
        user_id,
        f"apple_iap:{transaction_id}",
    )
    return str(row["id"]) if row and row.get("id") else None


async def confirm_subscription_purchase(
    conn,
    *,
    user_id: UUID,
    auth_country_code: Optional[str],
    payload: AppleSubscriptionConfirmIn,
) -> AppleSubscriptionConfirmOut:
    require_apple_iap_enabled()
    verification_mode = apple_iap_verification_mode()
    decoded_txn = jws_to_payload_dict(payload.signed_transaction_info)
    if not decoded_txn:
        raise HTTPException(status_code=422, detail="invalid_apple_signed_transaction")
    decoded_renewal = jws_to_payload_dict(payload.signed_renewal_info) if payload.signed_renewal_info else {}

    apple_product_id = str(decoded_txn.get("productId") or payload.apple_product_id or "").strip()
    if not apple_product_id:
        raise HTTPException(status_code=422, detail="apple_product_id_missing")

    transaction_id = str(decoded_txn.get("transactionId") or payload.transaction_id or "").strip()
    original_transaction_id = str(decoded_txn.get("originalTransactionId") or payload.original_transaction_id or "").strip()
    if not transaction_id or not original_transaction_id:
        raise HTTPException(status_code=422, detail="apple_subscription_ids_missing")

    app_account_token = as_uuid_or_none(decoded_txn.get("appAccountToken") or payload.app_account_token)
    if app_account_token and app_account_token != user_id:
        raise HTTPException(status_code=403, detail="apple_app_account_token_mismatch")

    currency = normalize_currency(payload.currency or settings.currency_for_country(auth_country_code))
    country_code = coerce_transaction_country_code(payload.country_code, auth_country_code, payload.storefront or decoded_txn.get("storefront"))
    purchase_date = apple_epoch_ms_to_datetime(decoded_txn.get("purchaseDate"))
    expires_date = apple_epoch_ms_to_datetime(decoded_txn.get("expiresDate"))
    environment = normalize_apple_environment(decoded_txn.get("environment") or payload.environment)
    storefront = str(payload.storefront or decoded_txn.get("storefront") or "").strip() or None
    storefront_id = str(decoded_txn.get("storefrontId") or "").strip() or None
    ownership_type = str(decoded_txn.get("inAppOwnershipType") or "").strip() or None
    transaction_reason = str(decoded_txn.get("transactionReason") or "").strip() or None

    try:
        mapping = await resolve_apple_product_mapping(
            conn,
            apple_product_id=apple_product_id,
            expected_product_type="subscription",
            currency=currency,
            country_code=country_code,
        )
        internal_plan_code = str(mapping.get("internal_plan_code") or "").strip()
        if not internal_plan_code:
            raise HTTPException(status_code=422, detail="apple_subscription_plan_mapping_missing")

        plan_profile = await fetch_plan_profile(conn, internal_plan_code=internal_plan_code)
        mapping_md = as_dict_loose(mapping.get("metadata_json"))
        tier_code = str(plan_profile.get("tier_code") or mapping_md.get("tier_code") or "").strip().lower()
        interval_code = str(plan_profile.get("interval_code") or mapping_md.get("billing_interval") or "monthly").strip().lower()
        monthly_grant_credits = int(plan_profile.get("monthly_grant_credits") or 0)

        trial_offer = str(decoded_txn.get("offerType") or "").strip()
        subscription_state = "trialing" if trial_offer else "active"
        entitlement_state = "active"
        cancel_at_period_end = str(decoded_renewal.get("autoRenewStatus") or "").strip() == "0"

        metadata_json = {
            "provider": "apple_iap",
            "apple_product_id": apple_product_id,
            "transaction_id": transaction_id,
            "original_transaction_id": original_transaction_id,
            "verification_mode": verification_mode,
            "environment": environment,
            "country_code": country_code,
            "currency": currency,
        }

        await upsert_apple_transaction_audit(
            conn,
            user_id=user_id,
            apple_product_id=apple_product_id,
            product_type="subscription",
            transaction_id=transaction_id,
            original_transaction_id=original_transaction_id,
            app_account_token=app_account_token,
            environment=environment,
            currency=currency,
            country_code=country_code,
            storefront=storefront,
            storefront_id=storefront_id,
            purchase_date=purchase_date,
            expires_date=expires_date,
            ownership_type=ownership_type,
            transaction_reason=transaction_reason,
            raw_signed_transaction=payload.signed_transaction_info,
            raw_signed_renewal=payload.signed_renewal_info,
            raw_decoded_json={"transaction": decoded_txn, "renewal": decoded_renewal},
        )

        await upsert_apple_subscription_row(
            conn,
            user_id=user_id,
            app_account_token=app_account_token,
            original_transaction_id=original_transaction_id,
            apple_product_id=apple_product_id,
            internal_plan_code=internal_plan_code,
            subscription_state=subscription_state,
            entitlement_state=entitlement_state,
            current_period_start=purchase_date,
            current_period_end=expires_date,
            cancel_at_period_end=cancel_at_period_end,
            canceled_at=None,
            trial_start=purchase_date if trial_offer else None,
            trial_end=expires_date if trial_offer else None,
            metadata_json=metadata_json,
        )

        cycle_key = apple_cycle_key(
            interval_code=interval_code,
            period_start=purchase_date,
            period_end=expires_date,
        )
        await apply_subscription_entitlement(
            conn,
            user_id=user_id,
            tier_code=tier_code,
            internal_plan_code=internal_plan_code,
            cycle_key=cycle_key,
            monthly_grant_credits=monthly_grant_credits,
            current_period_start=purchase_date,
            source="apple_iap",
            metadata_json=metadata_json,
        )
        await reconcile_included_plan_credits(
            conn,
            user_id=user_id,
            plan_code=internal_plan_code,
            tier_code=tier_code,
            included_credit_cap=monthly_grant_credits,
            cycle_key=cycle_key,
            current_period_start=purchase_date,
            current_period_end=expires_date,
            source="apple_iap",
            metadata_json=metadata_json,
        )

        return AppleSubscriptionConfirmOut(
            apple_product_id=apple_product_id,
            plan_code=internal_plan_code,
            tier_code=tier_code,
            subscription_state=subscription_state,
            entitlement_state=entitlement_state,
            current_period_start=purchase_date.isoformat() if purchase_date else None,
            current_period_end=expires_date.isoformat() if expires_date else None,
            verification_mode=verification_mode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _raise_from_repo_error(exc)


async def confirm_credit_purchase(
    conn,
    *,
    user_id: UUID,
    auth_country_code: Optional[str],
    payload: AppleCreditsConfirmIn,
) -> AppleCreditsConfirmOut:
    require_apple_iap_enabled()
    verification_mode = apple_iap_verification_mode()
    decoded_txn = jws_to_payload_dict(payload.signed_transaction_info)
    if not decoded_txn:
        raise HTTPException(status_code=422, detail="invalid_apple_signed_transaction")

    apple_product_id = str(decoded_txn.get("productId") or payload.apple_product_id or "").strip()
    if not apple_product_id:
        raise HTTPException(status_code=422, detail="apple_product_id_missing")
    transaction_id = str(decoded_txn.get("transactionId") or payload.transaction_id or "").strip()
    if not transaction_id:
        raise HTTPException(status_code=422, detail="apple_transaction_id_missing")

    original_transaction_id = str(decoded_txn.get("originalTransactionId") or payload.original_transaction_id or "").strip() or None
    app_account_token = as_uuid_or_none(decoded_txn.get("appAccountToken") or payload.app_account_token)
    if app_account_token and app_account_token != user_id:
        raise HTTPException(status_code=403, detail="apple_app_account_token_mismatch")

    currency = normalize_currency(payload.currency or settings.currency_for_country(auth_country_code))
    country_code = coerce_transaction_country_code(payload.country_code, auth_country_code, payload.storefront or decoded_txn.get("storefront"))
    purchase_date = apple_epoch_ms_to_datetime(decoded_txn.get("purchaseDate"))
    environment = normalize_apple_environment(decoded_txn.get("environment") or payload.environment)
    storefront = str(payload.storefront or decoded_txn.get("storefront") or "").strip() or None
    storefront_id = str(decoded_txn.get("storefrontId") or "").strip() or None

    try:
        mapping = await resolve_apple_product_mapping(
            conn,
            apple_product_id=apple_product_id,
            expected_product_type="consumable",
            currency=currency,
            country_code=country_code,
        )
        internal_pack_code = str(mapping.get("internal_pack_code") or "").strip()
        if not internal_pack_code:
            raise HTTPException(status_code=422, detail="apple_consumable_pack_mapping_missing")

        inserted = await upsert_apple_transaction_audit(
            conn,
            user_id=user_id,
            apple_product_id=apple_product_id,
            product_type="consumable",
            transaction_id=transaction_id,
            original_transaction_id=original_transaction_id,
            app_account_token=app_account_token,
            environment=environment,
            currency=currency,
            country_code=country_code,
            storefront=storefront,
            storefront_id=storefront_id,
            purchase_date=purchase_date,
            expires_date=None,
            ownership_type=str(decoded_txn.get("inAppOwnershipType") or "").strip() or None,
            transaction_reason=str(decoded_txn.get("transactionReason") or "").strip() or None,
            raw_signed_transaction=payload.signed_transaction_info,
            raw_signed_renewal=None,
            raw_decoded_json={"transaction": decoded_txn},
        )

        pack_profile = await fetch_credit_pack_profile(conn, internal_pack_code=internal_pack_code)
        grant_result = {"wallet_order_id": None, "granted_credits": int(pack_profile.get("credits") or 0)}

        if inserted:
            grant_result = await best_effort_grant_apple_credits(
                conn,
                user_id=user_id,
                internal_pack_code=internal_pack_code,
                pack_profile=pack_profile,
                transaction_id=transaction_id,
                original_transaction_id=original_transaction_id,
                apple_product_id=apple_product_id,
                currency=currency,
                country_code=country_code,
                storefront=storefront,
            )
        else:
            existing_wallet_order_id = await _find_existing_apple_wallet_order_id(
                conn,
                user_id=user_id,
                transaction_id=transaction_id,
            )
            if existing_wallet_order_id:
                grant_result["wallet_order_id"] = existing_wallet_order_id

        # Canonical rule: Apple, Stripe, and any future gateway only verify payment.
        # Actual wallet credit fulfillment is centralized here and is idempotent.
        if grant_result.get("wallet_order_id"):
            fulfillment_result = await fulfill_wallet_topup_order(
                conn,
                wallet_order_id=UUID(str(grant_result["wallet_order_id"])),
                gateway_provider="apple_iap",
                gateway_checkout_session_id=None,
                source_metadata={
                    "apple_product_id": apple_product_id,
                    "internal_pack_code": internal_pack_code,
                    "transaction_id": transaction_id,
                    "original_transaction_id": original_transaction_id,
                    "country_code": country_code,
                    "storefront": storefront,
                    "environment": environment,
                    "purchase_date": purchase_date.isoformat() if purchase_date else None,
                },
            )
            if fulfillment_result:
                grant_result["granted_credits"] = int(fulfillment_result.get("credits_to_grant") or grant_result.get("granted_credits") or 0)
                grant_result["fulfillment_state"] = str(fulfillment_result.get("fulfillment_state") or "granted")
                grant_result["ledger_entry_id"] = fulfillment_result.get("ledger_entry_id")
                grant_result["credit_lot_id"] = fulfillment_result.get("credit_lot_id")

        return AppleCreditsConfirmOut(
            apple_product_id=apple_product_id,
            internal_pack_code=internal_pack_code,
            granted_credits=int(grant_result.get("granted_credits") or 0),
            wallet_order_id=grant_result.get("wallet_order_id"),
            verification_mode=verification_mode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _raise_from_repo_error(exc)


async def process_notification(
    conn,
    *,
    payload: AppleNotificationIn,
) -> AppleNotificationOut:
    require_apple_iap_enabled()
    verification_mode = apple_iap_verification_mode()
    decoded_notification = jws_to_payload_dict(payload.signedPayload)
    if not decoded_notification:
        raise HTTPException(status_code=422, detail="invalid_apple_notification_payload")

    data = as_dict_loose(decoded_notification.get("data"))
    signed_txn = str(data.get("signedTransactionInfo") or "").strip()
    signed_renewal = str(data.get("signedRenewalInfo") or "").strip()
    decoded_txn = jws_to_payload_dict(signed_txn) if signed_txn else {}
    decoded_renewal = jws_to_payload_dict(signed_renewal) if signed_renewal else {}

    notification_uuid = str(decoded_notification.get("notificationUUID") or "").strip()
    notification_type = str(decoded_notification.get("notificationType") or "").strip().upper()
    subtype = str(decoded_notification.get("subtype") or "").strip().upper() or None
    environment = normalize_apple_environment(decoded_notification.get("environment"))
    transaction_id = str(decoded_txn.get("transactionId") or "").strip() or None
    original_transaction_id = str(decoded_txn.get("originalTransactionId") or "").strip() or None
    app_account_token = as_uuid_or_none(decoded_txn.get("appAccountToken"))

    if not notification_uuid:
        raise HTTPException(status_code=422, detail="apple_notification_uuid_missing")

    try:
        inserted = await record_apple_notification_event(
            conn,
            notification_uuid=notification_uuid,
            notification_type=notification_type or "UNKNOWN",
            subtype=subtype,
            environment=environment,
            signed_payload=payload.signedPayload,
            decoded_payload_json={"notification": decoded_notification, "transaction": decoded_txn, "renewal": decoded_renewal},
            transaction_id=transaction_id,
            original_transaction_id=original_transaction_id,
            app_account_token=app_account_token,
        )

        if inserted and original_transaction_id and decoded_txn:
            apple_product_id = str(decoded_txn.get("productId") or "").strip()
            transaction_country = coerce_transaction_country_code(None, None, decoded_txn.get("storefront"))
            currency = "INR" if transaction_country == "IN" else "USD"

            mapping = await resolve_apple_product_mapping(
                conn,
                apple_product_id=apple_product_id,
                expected_product_type="subscription",
                currency=currency,
                country_code=transaction_country,
            )
            internal_plan_code = str(mapping.get("internal_plan_code") or "").strip()
            if internal_plan_code:
                plan_profile = await fetch_plan_profile(conn, internal_plan_code=internal_plan_code)
                mapping_md = as_dict_loose(mapping.get("metadata_json"))
                tier_code = str(plan_profile.get("tier_code") or mapping_md.get("tier_code") or "").strip().lower()
                interval_code = str(plan_profile.get("interval_code") or mapping_md.get("billing_interval") or "monthly").strip().lower()
                monthly_grant_credits = int(plan_profile.get("monthly_grant_credits") or 0)
                purchase_date = apple_epoch_ms_to_datetime(decoded_txn.get("purchaseDate"))
                expires_date = apple_epoch_ms_to_datetime(decoded_txn.get("expiresDate"))
                state = derive_subscription_state_from_notification(notification_type, subtype, decoded_txn)

                resolved_user_id = app_account_token
                if resolved_user_id is None and original_transaction_id:
                    resolved_user_id = await find_subscription_user_id(conn, original_transaction_id=original_transaction_id)

                if resolved_user_id is not None:
                    metadata_json = {
                        "provider": "apple_iap",
                        "apple_product_id": apple_product_id,
                        "transaction_id": transaction_id,
                        "original_transaction_id": original_transaction_id,
                        "notification_uuid": notification_uuid,
                        "notification_type": notification_type,
                        "notification_subtype": subtype,
                        "verification_mode": verification_mode,
                        "environment": environment,
                        "currency": currency,
                        "country_code": transaction_country,
                    }

                    await upsert_apple_subscription_row(
                        conn,
                        user_id=resolved_user_id,
                        app_account_token=app_account_token,
                        original_transaction_id=original_transaction_id,
                        apple_product_id=apple_product_id,
                        internal_plan_code=internal_plan_code,
                        subscription_state=str(state["subscription_state"]),
                        entitlement_state=str(state["entitlement_state"]),
                        current_period_start=purchase_date,
                        current_period_end=expires_date,
                        cancel_at_period_end=bool(state["cancel_at_period_end"]),
                        canceled_at=datetime.now(timezone.utc) if str(state["subscription_state"]) == "canceled" else None,
                        trial_start=purchase_date if str(state["subscription_state"]) == "trialing" else None,
                        trial_end=expires_date if str(state["subscription_state"]) == "trialing" else None,
                        metadata_json=metadata_json,
                    )

                    if bool(state.get("revert_to_free")):
                        cycle_key = apple_cycle_key(
                            interval_code=interval_code,
                            period_start=purchase_date,
                            period_end=expires_date,
                        )
                        await revert_user_to_free_entitlement(
                            conn,
                            user_id=resolved_user_id,
                            source="apple_iap_notification",
                            metadata_json=metadata_json,
                        )
                        await reconcile_included_plan_credits(
                            conn,
                            user_id=resolved_user_id,
                            plan_code="free",
                            tier_code="free",
                            included_credit_cap=None,
                            cycle_key=cycle_key,
                            current_period_start=purchase_date,
                            current_period_end=expires_date,
                            source="apple_iap_notification_revert_to_free",
                            metadata_json=metadata_json,
                        )
                    else:
                        cycle_key = apple_cycle_key(
                            interval_code=interval_code,
                            period_start=purchase_date,
                            period_end=expires_date,
                        )
                        await apply_subscription_entitlement(
                            conn,
                            user_id=resolved_user_id,
                            tier_code=tier_code,
                            internal_plan_code=internal_plan_code,
                            cycle_key=cycle_key,
                            monthly_grant_credits=monthly_grant_credits,
                            current_period_start=purchase_date,
                            source="apple_iap_notification",
                            metadata_json=metadata_json,
                        )
                        await reconcile_included_plan_credits(
                            conn,
                            user_id=resolved_user_id,
                            plan_code=internal_plan_code,
                            tier_code=tier_code,
                            included_credit_cap=monthly_grant_credits,
                            cycle_key=cycle_key,
                            current_period_start=purchase_date,
                            current_period_end=expires_date,
                            source="apple_iap_notification",
                            metadata_json=metadata_json,
                        )

        return AppleNotificationOut(
            notification_uuid=notification_uuid,
            notification_type=notification_type or None,
            subtype=subtype,
            processing_status="processed",
            verification_mode=verification_mode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _raise_from_repo_error(exc)
