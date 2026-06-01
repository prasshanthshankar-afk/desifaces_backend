from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException

from app.config import settings
from app.repo.google_play_iap_repo import (
    apply_subscription_entitlement,
    as_dict_loose,
    best_effort_grant_google_credits,
    fetch_credit_pack_profile,
    fetch_plan_profile,
    find_existing_google_wallet_order_id,
    find_purchase_user_id,
    find_subscription_user_id,
    mark_google_purchase_fulfilled,
    record_google_notification_event,
    resolve_google_product_mapping,
    revert_user_to_free_entitlement,
    sha256_hex,
    upsert_google_purchase_audit,
    upsert_google_subscription_row,
)
from app.schemas.google_play import (
    GoogleCreditsConfirmIn,
    GoogleCreditsConfirmOut,
    GoogleNotificationIn,
    GoogleNotificationOut,
    GoogleSubscriptionConfirmIn,
    GoogleSubscriptionConfirmOut,
)
from app.services.payments.wallet_fulfillment_service import fulfill_wallet_topup_order
from app.services.entitlements.plan_credit_reconciliation_service import reconcile_included_plan_credits


ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
ANDROID_PUBLISHER_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) if os.getenv(name) is not None else getattr(settings, name, default)).strip().lower()
    if raw in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "f", "no", "n", "off"}:
        return False
    return bool(default)


def google_play_iap_enabled() -> bool:
    return _env_bool("DF_GOOGLE_PLAY_IAP_ENABLE", False)


def google_play_validate_purchases_enabled() -> bool:
    return _env_bool("GOOGLE_PLAY_VALIDATE_PURCHASES", True)


def google_play_acknowledge_purchases_enabled() -> bool:
    return _env_bool("GOOGLE_PLAY_ACKNOWLEDGE_PURCHASES", True)


def google_play_verification_mode() -> str:
    if not google_play_validate_purchases_enabled():
        return "payload_only"
    return str(
        os.getenv("GOOGLE_PLAY_VERIFICATION_MODE")
        or getattr(settings, "GOOGLE_PLAY_VERIFICATION_MODE", "google_api")
        or "google_api"
    ).strip().lower()


def require_google_play_iap_enabled() -> None:
    if not google_play_iap_enabled():
        raise HTTPException(status_code=503, detail="google_play_iap_disabled")


def configured_package_name() -> str:
    return str(
        os.getenv("GOOGLE_PLAY_PACKAGE_NAME")
        or getattr(settings, "GOOGLE_PLAY_PACKAGE_NAME", "")
        or ""
    ).strip()


def normalize_package_name(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    fallback = configured_package_name()
    package_name = raw or fallback
    if not package_name:
        raise HTTPException(status_code=503, detail="google_play_package_name_missing")
    if fallback and package_name != fallback:
        raise HTTPException(status_code=403, detail="google_play_package_name_mismatch")
    return package_name


def normalize_currency(value: Optional[str]) -> str:
    return str(value or getattr(settings, "DF_WALLET_TOPUP_CURRENCY", "USD") or "USD").strip().upper()


def normalize_country_code(value: Optional[str]) -> str:
    return str(value or "").strip().upper()


def _record_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        pass
    try:
        value = row.get(key)
        return default if value is None else value
    except Exception:
        return default


def _deep_get(d: Dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, "", 0, "0"):
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # Google product purchases use milliseconds.
    if raw.isdigit():
        try:
            ivalue = int(raw)
            if ivalue > 0:
                # Millis if large, seconds if small.
                if ivalue > 10_000_000_000:
                    return datetime.fromtimestamp(ivalue / 1000.0, tz=timezone.utc)
                return datetime.fromtimestamp(ivalue, tz=timezone.utc)
        except Exception:
            return None

    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def google_cycle_key(*, interval_code: str, period_start: Optional[datetime], period_end: Optional[datetime]) -> str:
    anchor = period_start or period_end or datetime.now(timezone.utc)
    interval = str(interval_code or "monthly").strip().lower()
    if interval == "yearly":
        return f"{anchor.year:04d}"
    return f"{anchor.year:04d}-{anchor.month:02d}"


def _get_purchase_token_hash(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="google_purchase_token_missing")
    return sha256_hex(raw)


def _service_account_json_or_path() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    raw_json = str(
        os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON")
        or getattr(settings, "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "")
        or ""
    ).strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return parsed, None
        except Exception:
            raise HTTPException(status_code=503, detail="google_play_service_account_json_invalid")

    path = str(
        os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH")
        or os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE")
        or getattr(settings, "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH", "")
        or ""
    ).strip()
    if path:
        return None, path
    return None, None


def _google_access_token_sync() -> str:
    info, path = _service_account_json_or_path()
    if not info and not path:
        raise HTTPException(status_code=503, detail="google_play_service_account_missing")

    try:
        from google.oauth2 import service_account  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
    except Exception:
        raise HTTPException(status_code=503, detail="google_auth_dependency_missing")

    try:
        if info:
            credentials = service_account.Credentials.from_service_account_info(info, scopes=[ANDROID_PUBLISHER_SCOPE])
        else:
            credentials = service_account.Credentials.from_service_account_file(path, scopes=[ANDROID_PUBLISHER_SCOPE])
        credentials.refresh(Request())
        token = str(credentials.token or "").strip()
        if not token:
            raise RuntimeError("empty_google_access_token")
        return token
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"google_play_auth_failed:{exc}")


async def _google_access_token() -> str:
    return await asyncio.to_thread(_google_access_token_sync)


def _google_json_request_sync(method: str, url: str, *, access_token: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = None if body is None else json.dumps(body, default=str).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {}
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            raw = ""
        raise HTTPException(status_code=502, detail=f"google_play_api_error:{exc.code}:{raw[:500]}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"google_play_api_request_failed:{exc}")


async def _google_json_request(method: str, url: str, *, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    access_token = await _google_access_token()
    return await asyncio.to_thread(_google_json_request_sync, method, url, access_token=access_token, body=body)


def _quote_path(value: str) -> str:
    return urllib.parse.quote(str(value or ""), safe="")


async def fetch_subscription_from_google_api(*, package_name: str, purchase_token: str) -> Dict[str, Any]:
    url = (
        f"{ANDROID_PUBLISHER_BASE}/applications/{_quote_path(package_name)}"
        f"/purchases/subscriptionsv2/tokens/{_quote_path(purchase_token)}"
    )
    return await _google_json_request("GET", url)


async def fetch_product_from_google_api(*, package_name: str, product_id: str, purchase_token: str) -> Dict[str, Any]:
    url = (
        f"{ANDROID_PUBLISHER_BASE}/applications/{_quote_path(package_name)}"
        f"/purchases/products/{_quote_path(product_id)}/tokens/{_quote_path(purchase_token)}"
    )
    return await _google_json_request("GET", url)


async def acknowledge_subscription_best_effort(*, package_name: str, subscription_id: str, purchase_token: str) -> Optional[str]:
    if not google_play_acknowledge_purchases_enabled():
        return None
    url = (
        f"{ANDROID_PUBLISHER_BASE}/applications/{_quote_path(package_name)}"
        f"/purchases/subscriptions/{_quote_path(subscription_id)}/tokens/{_quote_path(purchase_token)}:acknowledge"
    )
    try:
        await _google_json_request("POST", url, body={})
        return "ACKNOWLEDGED"
    except Exception:
        return None


async def acknowledge_product_best_effort(*, package_name: str, product_id: str, purchase_token: str) -> Optional[str]:
    if not google_play_acknowledge_purchases_enabled():
        return None
    url = (
        f"{ANDROID_PUBLISHER_BASE}/applications/{_quote_path(package_name)}"
        f"/purchases/products/{_quote_path(product_id)}/tokens/{_quote_path(purchase_token)}:acknowledge"
    )
    try:
        await _google_json_request("POST", url, body={})
        return "ACKNOWLEDGED"
    except Exception:
        return None


async def consume_product_best_effort(*, package_name: str, product_id: str, purchase_token: str) -> Optional[str]:
    if not google_play_acknowledge_purchases_enabled():
        return None
    url = (
        f"{ANDROID_PUBLISHER_BASE}/applications/{_quote_path(package_name)}"
        f"/purchases/products/{_quote_path(product_id)}/tokens/{_quote_path(purchase_token)}:consume"
    )
    try:
        await _google_json_request("POST", url, body={})
        return "CONSUMED"
    except Exception:
        return None


def subscription_line_item(data: Dict[str, Any]) -> Dict[str, Any]:
    items = data.get("lineItems")
    if isinstance(items, list) and items:
        first = items[0]
        return first if isinstance(first, dict) else {}
    return {}


def subscription_product_id(data: Dict[str, Any], fallback: Optional[str]) -> str:
    line = subscription_line_item(data)
    return str(line.get("productId") or data.get("productId") or fallback or "").strip()


def subscription_base_plan_id(data: Dict[str, Any], fallback: Optional[str]) -> str:
    line = subscription_line_item(data)
    offer = as_dict_loose(line.get("offerDetails"))
    return str(
        offer.get("basePlanId")
        or line.get("basePlanId")
        or data.get("basePlanId")
        or fallback
        or ""
    ).strip()


def subscription_region_code(data: Dict[str, Any], fallback: Optional[str]) -> str:
    return normalize_country_code(data.get("regionCode") or data.get("countryCode") or fallback)


def subscription_order_id(data: Dict[str, Any], fallback: Optional[str]) -> Optional[str]:
    line = subscription_line_item(data)
    raw = line.get("latestSuccessfulOrderId") or data.get("latestOrderId") or data.get("orderId") or fallback
    return str(raw).strip() if raw else None


def subscription_period(data: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[datetime]]:
    line = subscription_line_item(data)
    start = parse_datetime(data.get("startTime") or line.get("startTime"))
    end = parse_datetime(line.get("expiryTime") or data.get("expiryTime"))
    return start, end


def subscription_linked_token_hash(data: Dict[str, Any]) -> Optional[str]:
    token = str(data.get("linkedPurchaseToken") or "").strip()
    return sha256_hex(token) if token else None


def subscription_ack_state(data: Dict[str, Any]) -> Optional[str]:
    raw = data.get("acknowledgementState")
    if raw is None:
        return None
    if str(raw) in {"1", "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"}:
        return "ACKNOWLEDGED"
    if str(raw) in {"0", "ACKNOWLEDGEMENT_STATE_PENDING"}:
        return "PENDING"
    return str(raw)


def derive_subscription_state(data: Dict[str, Any]) -> Dict[str, Any]:
    state = str(data.get("subscriptionState") or "").strip().upper()
    line = subscription_line_item(data)
    auto_plan = as_dict_loose(line.get("autoRenewingPlan"))
    auto_enabled = auto_plan.get("autoRenewEnabled")

    if state in {"SUBSCRIPTION_STATE_ACTIVE"}:
        return {
            "subscription_state": "active",
            "entitlement_state": "active",
            "cancel_at_period_end": bool(auto_enabled is False),
            "revert_to_free": False,
        }
    if state in {"SUBSCRIPTION_STATE_IN_GRACE_PERIOD"}:
        return {
            "subscription_state": "past_due",
            "entitlement_state": "grace",
            "cancel_at_period_end": False,
            "revert_to_free": False,
        }
    if state in {"SUBSCRIPTION_STATE_ON_HOLD", "SUBSCRIPTION_STATE_PAUSED"}:
        return {
            "subscription_state": "paused",
            "entitlement_state": "suspended",
            "cancel_at_period_end": False,
            "revert_to_free": False,
        }
    if state in {"SUBSCRIPTION_STATE_CANCELED"}:
        return {
            "subscription_state": "canceled",
            "entitlement_state": "inactive",
            "cancel_at_period_end": False,
            "revert_to_free": True,
        }
    if state in {"SUBSCRIPTION_STATE_EXPIRED"}:
        return {
            "subscription_state": "canceled",
            "entitlement_state": "inactive",
            "cancel_at_period_end": False,
            "revert_to_free": True,
        }
    if state in {"SUBSCRIPTION_STATE_PENDING"}:
        return {
            "subscription_state": "incomplete",
            "entitlement_state": "inactive",
            "cancel_at_period_end": False,
            "revert_to_free": False,
        }

    # Payload-only fallback for local tests.
    if not state:
        return {
            "subscription_state": "active",
            "entitlement_state": "active",
            "cancel_at_period_end": False,
            "revert_to_free": False,
        }

    return {
        "subscription_state": "incomplete",
        "entitlement_state": "inactive",
        "cancel_at_period_end": False,
        "revert_to_free": False,
    }


def product_purchase_state(data: Dict[str, Any]) -> str:
    raw = data.get("purchaseState")
    if raw is None:
        raw = data.get("purchase_state")
    if raw is None:
        return "PURCHASED"  # payload-only local test fallback
    if str(raw) in {"0", "PURCHASED", "purchased"}:
        return "PURCHASED"
    if str(raw) in {"1", "CANCELED", "cancelled", "canceled"}:
        return "CANCELED"
    if str(raw) in {"2", "PENDING", "pending"}:
        return "PENDING"
    return str(raw).upper()


def product_ack_state(data: Dict[str, Any]) -> Optional[str]:
    raw = data.get("acknowledgementState")
    if raw is None:
        raw = data.get("acknowledged")
        if raw is True:
            return "ACKNOWLEDGED"
        if raw is False:
            return "PENDING"
        return None
    if str(raw) in {"1", "ACKNOWLEDGED", "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"}:
        return "ACKNOWLEDGED"
    if str(raw) in {"0", "PENDING", "ACKNOWLEDGEMENT_STATE_PENDING"}:
        return "PENDING"
    return str(raw)


def product_consumption_state(data: Dict[str, Any]) -> Optional[str]:
    raw = data.get("consumptionState")
    if raw is None:
        raw = data.get("consumed")
        if raw is True:
            return "CONSUMED"
        if raw is False:
            return "NOT_CONSUMED"
        return None
    if str(raw) in {"1", "CONSUMED"}:
        return "CONSUMED"
    if str(raw) in {"0", "YET_TO_BE_CONSUMED", "NOT_CONSUMED"}:
        return "NOT_CONSUMED"
    return str(raw)


def product_purchase_time(data: Dict[str, Any]) -> Optional[datetime]:
    return parse_datetime(data.get("purchaseTimeMillis") or data.get("purchaseTime") or data.get("purchase_date"))


def _raise_from_repo_error(exc: Exception) -> None:
    detail = str(exc)
    if detail in {
        "google_play_iap_tables_missing",
        "google_play_iap_mappings_missing",
    }:
        raise HTTPException(status_code=503, detail=detail)
    if detail in {
        "google_play_product_not_mapped",
        "google_play_plan_profile_missing",
        "google_play_credit_pack_missing",
    }:
        raise HTTPException(status_code=422, detail=detail)
    raise exc


async def _verified_subscription_payload(
    *,
    package_name: str,
    purchase_token: str,
    client_payload: GoogleSubscriptionConfirmIn,
) -> Dict[str, Any]:
    if google_play_validate_purchases_enabled():
        return await fetch_subscription_from_google_api(package_name=package_name, purchase_token=purchase_token)

    raw = dict(client_payload.raw_purchase_json or {})
    raw.setdefault("subscriptionState", raw.get("subscriptionState") or "SUBSCRIPTION_STATE_ACTIVE")
    raw.setdefault("regionCode", client_payload.country_code)
    raw.setdefault("orderId", client_payload.order_id)
    if client_payload.product_id or client_payload.google_product_id or client_payload.base_plan_id:
        raw.setdefault(
            "lineItems",
            [
                {
                    "productId": client_payload.google_product_id or client_payload.product_id,
                    "offerDetails": {"basePlanId": client_payload.base_plan_id or ""},
                }
            ],
        )
    return raw


async def _verified_product_payload(
    *,
    package_name: str,
    product_id: str,
    purchase_token: str,
    client_payload: GoogleCreditsConfirmIn,
) -> Dict[str, Any]:
    if google_play_validate_purchases_enabled():
        return await fetch_product_from_google_api(
            package_name=package_name,
            product_id=product_id,
            purchase_token=purchase_token,
        )

    raw = dict(client_payload.raw_purchase_json or {})
    raw.setdefault("purchaseState", 0)
    raw.setdefault("orderId", client_payload.order_id)
    raw.setdefault("regionCode", client_payload.country_code)
    if client_payload.acknowledged is not None:
        raw.setdefault("acknowledgementState", 1 if client_payload.acknowledged else 0)
    if client_payload.consumed is not None:
        raw.setdefault("consumptionState", 1 if client_payload.consumed else 0)
    return raw


async def confirm_subscription_purchase(
    conn,
    *,
    user_id: UUID,
    auth_country_code: Optional[str],
    payload: GoogleSubscriptionConfirmIn,
) -> GoogleSubscriptionConfirmOut:
    require_google_play_iap_enabled()

    verification_mode = google_play_verification_mode()
    purchase_token = str(payload.purchase_token or "").strip()
    if not purchase_token:
        raise HTTPException(status_code=422, detail="google_purchase_token_missing")

    package_name = normalize_package_name(payload.package_name)
    purchase_token_hash = _get_purchase_token_hash(purchase_token)
    verified = await _verified_subscription_payload(
        package_name=package_name,
        purchase_token=purchase_token,
        client_payload=payload,
    )

    google_product_id = subscription_product_id(verified, payload.google_product_id or payload.product_id)
    if not google_product_id:
        raise HTTPException(status_code=422, detail="google_product_id_missing")

    base_plan_id = subscription_base_plan_id(verified, payload.base_plan_id)
    region_code = subscription_region_code(verified, payload.country_code or auth_country_code)
    currency = normalize_currency(payload.currency or settings.currency_for_country(region_code or auth_country_code))
    start_time, end_time = subscription_period(verified)
    state = derive_subscription_state(verified)
    order_id = subscription_order_id(verified, payload.order_id)
    ack_state = subscription_ack_state(verified)
    linked_hash = subscription_linked_token_hash(verified)

    try:
        mapping = await resolve_google_product_mapping(
            conn,
            google_product_id=google_product_id,
            expected_product_type="subscription",
            currency=currency,
            country_code=region_code,
            base_plan_id=base_plan_id,
        )
        internal_plan_code = str(mapping.get("internal_plan_code") or "").strip()
        if not internal_plan_code:
            raise HTTPException(status_code=422, detail="google_subscription_plan_mapping_missing")

        plan_profile = await fetch_plan_profile(conn, internal_plan_code=internal_plan_code)
        mapping_md = as_dict_loose(mapping.get("metadata_json"))
        tier_code = str(plan_profile.get("tier_code") or mapping_md.get("tier_code") or "").strip().lower()
        interval_code = str(plan_profile.get("interval_code") or mapping_md.get("billing_interval") or "monthly").strip().lower()
        grant_credits = int(plan_profile.get("monthly_grant_credits") or 0)

        metadata_json = {
            "provider": "google_play",
            "google_product_id": google_product_id,
            "base_plan_id": base_plan_id,
            "purchase_token_hash": purchase_token_hash,
            "linked_purchase_token_hash": linked_hash,
            "order_id": order_id,
            "verification_mode": verification_mode,
            "package_name": package_name,
            "country_code": region_code,
            "currency": currency,
        }

        await upsert_google_purchase_audit(
            conn,
            user_id=user_id,
            google_product_id=google_product_id,
            base_plan_id=base_plan_id,
            product_type="subscription",
            package_name=package_name,
            purchase_token_hash=purchase_token_hash,
            order_id=order_id,
            linked_purchase_token_hash=linked_hash,
            purchase_state=None,
            acknowledgement_state=ack_state,
            consumption_state=None,
            subscription_state=str(verified.get("subscriptionState") or ""),
            internal_pack_code=None,
            internal_plan_code=internal_plan_code,
            raw_purchase_json={"subscription": verified},
            fulfillment_state=str(state["entitlement_state"]),
        )

        await upsert_google_subscription_row(
            conn,
            user_id=user_id,
            purchase_token_hash=purchase_token_hash,
            linked_purchase_token_hash=linked_hash,
            google_product_id=google_product_id,
            base_plan_id=base_plan_id,
            internal_plan_code=internal_plan_code,
            subscription_state=str(state["subscription_state"]),
            entitlement_state=str(state["entitlement_state"]),
            current_period_start=start_time,
            current_period_end=end_time,
            cancel_at_period_end=bool(state["cancel_at_period_end"]),
            canceled_at=datetime.now(timezone.utc) if str(state["subscription_state"]) == "canceled" else None,
            trial_start=None,
            trial_end=None,
            metadata_json=metadata_json,
        )

        if bool(state.get("revert_to_free")):
            cycle_key = google_cycle_key(
                interval_code=interval_code,
                period_start=start_time,
                period_end=end_time,
            )
            await revert_user_to_free_entitlement(
                conn,
                user_id=user_id,
                source="google_play",
                metadata_json=metadata_json,
            )
            await reconcile_included_plan_credits(
                conn,
                user_id=user_id,
                plan_code="free",
                tier_code="free",
                included_credit_cap=None,
                cycle_key=cycle_key,
                current_period_start=start_time,
                current_period_end=end_time,
                source="google_play_revert_to_free",
                metadata_json=metadata_json,
            )
        elif str(state["entitlement_state"]) in {"active", "grace"}:
            cycle_key = google_cycle_key(
                interval_code=interval_code,
                period_start=start_time,
                period_end=end_time,
            )
            await apply_subscription_entitlement(
                conn,
                user_id=user_id,
                tier_code=tier_code,
                internal_plan_code=internal_plan_code,
                cycle_key=cycle_key,
                grant_credits=grant_credits,
                current_period_start=start_time,
                source="google_play",
                metadata_json=metadata_json,
            )
            await reconcile_included_plan_credits(
                conn,
                user_id=user_id,
                plan_code=internal_plan_code,
                tier_code=tier_code,
                included_credit_cap=grant_credits,
                cycle_key=cycle_key,
                current_period_start=start_time,
                current_period_end=end_time,
                source="google_play",
                metadata_json=metadata_json,
            )

        ack_after = await acknowledge_subscription_best_effort(
            package_name=package_name,
            subscription_id=google_product_id,
            purchase_token=purchase_token,
        )
        if ack_after:
            ack_state = ack_after

        return GoogleSubscriptionConfirmOut(
            google_product_id=google_product_id,
            base_plan_id=base_plan_id or None,
            plan_code=internal_plan_code,
            tier_code=tier_code,
            subscription_state=str(state["subscription_state"]),
            entitlement_state=str(state["entitlement_state"]),
            current_period_start=start_time.isoformat() if start_time else None,
            current_period_end=end_time.isoformat() if end_time else None,
            purchase_token_hash=purchase_token_hash,
            verification_mode=verification_mode,
            acknowledgement_state=ack_state,
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
    payload: GoogleCreditsConfirmIn,
) -> GoogleCreditsConfirmOut:
    require_google_play_iap_enabled()

    verification_mode = google_play_verification_mode()
    purchase_token = str(payload.purchase_token or "").strip()
    if not purchase_token:
        raise HTTPException(status_code=422, detail="google_purchase_token_missing")
    purchase_token_hash = _get_purchase_token_hash(purchase_token)
    package_name = normalize_package_name(payload.package_name)

    google_product_id = str(payload.google_product_id or payload.product_id or "").strip()
    if not google_product_id:
        raise HTTPException(status_code=422, detail="google_product_id_missing")

    verified = await _verified_product_payload(
        package_name=package_name,
        product_id=google_product_id,
        purchase_token=purchase_token,
        client_payload=payload,
    )
    purchase_state = product_purchase_state(verified)
    if purchase_state != "PURCHASED":
        raise HTTPException(status_code=409, detail=f"google_product_purchase_not_purchased:{purchase_state}")

    region_code = normalize_country_code(verified.get("regionCode") or payload.country_code or auth_country_code)
    currency = normalize_currency(payload.currency or settings.currency_for_country(region_code or auth_country_code))
    order_id = str(verified.get("orderId") or payload.order_id or "").strip() or None
    purchase_time = product_purchase_time(verified)
    ack_state = product_ack_state(verified)
    consumption_state = product_consumption_state(verified)

    try:
        mapping = await resolve_google_product_mapping(
            conn,
            google_product_id=google_product_id,
            expected_product_type="consumable",
            currency=currency,
            country_code=region_code,
            base_plan_id="",
        )
        internal_pack_code = str(mapping.get("internal_pack_code") or "").strip()
        if not internal_pack_code:
            raise HTTPException(status_code=422, detail="google_consumable_pack_mapping_missing")

        inserted = await upsert_google_purchase_audit(
            conn,
            user_id=user_id,
            google_product_id=google_product_id,
            base_plan_id="",
            product_type="consumable",
            package_name=package_name,
            purchase_token_hash=purchase_token_hash,
            order_id=order_id,
            linked_purchase_token_hash=None,
            purchase_state=purchase_state,
            acknowledgement_state=ack_state,
            consumption_state=consumption_state,
            subscription_state=None,
            internal_pack_code=internal_pack_code,
            internal_plan_code=None,
            raw_purchase_json={"product": verified, "purchase_time": purchase_time.isoformat() if purchase_time else None},
            fulfillment_state="pending",
        )

        pack_profile = await fetch_credit_pack_profile(conn, internal_pack_code=internal_pack_code)
        grant_result = {"wallet_order_id": None, "granted_credits": int(pack_profile.get("credits") or 0)}

        if inserted:
            grant_result = await best_effort_grant_google_credits(
                conn,
                user_id=user_id,
                internal_pack_code=internal_pack_code,
                pack_profile=pack_profile,
                purchase_token_hash=purchase_token_hash,
                order_id=order_id,
                google_product_id=google_product_id,
                package_name=package_name,
                currency=currency,
                country_code=region_code,
            )
        else:
            existing_wallet_order_id = await find_existing_google_wallet_order_id(
                conn,
                user_id=user_id,
                purchase_token_hash=purchase_token_hash,
            )
            if existing_wallet_order_id:
                grant_result["wallet_order_id"] = existing_wallet_order_id

        if grant_result.get("wallet_order_id"):
            fulfillment_result = await fulfill_wallet_topup_order(
                conn,
                wallet_order_id=UUID(str(grant_result["wallet_order_id"])),
                gateway_provider="google_play",
                gateway_checkout_session_id=None,
                gateway_transaction_id=purchase_token_hash,
                source_metadata={
                    "google_product_id": google_product_id,
                    "internal_pack_code": internal_pack_code,
                    "purchase_token_hash": purchase_token_hash,
                    "order_id": order_id,
                    "package_name": package_name,
                    "country_code": region_code,
                    "purchase_time": purchase_time.isoformat() if purchase_time else None,
                },
            )
            if fulfillment_result:
                grant_result["granted_credits"] = int(fulfillment_result.get("credits_to_grant") or grant_result.get("granted_credits") or 0)
                grant_result["fulfillment_state"] = str(fulfillment_result.get("fulfillment_state") or "granted")
                grant_result["ledger_entry_id"] = fulfillment_result.get("ledger_entry_id")
                grant_result["credit_lot_id"] = fulfillment_result.get("credit_lot_id")
                await mark_google_purchase_fulfilled(
                    conn,
                    package_name=package_name,
                    google_product_id=google_product_id,
                    purchase_token_hash=purchase_token_hash,
                    fulfillment_state="granted",
                )

        consumed_after = await consume_product_best_effort(
            package_name=package_name,
            product_id=google_product_id,
            purchase_token=purchase_token,
        )
        if consumed_after:
            consumption_state = consumed_after
        else:
            ack_after = await acknowledge_product_best_effort(
                package_name=package_name,
                product_id=google_product_id,
                purchase_token=purchase_token,
            )
            if ack_after:
                ack_state = ack_after

        return GoogleCreditsConfirmOut(
            google_product_id=google_product_id,
            internal_pack_code=internal_pack_code,
            granted_credits=int(grant_result.get("granted_credits") or 0),
            wallet_order_id=grant_result.get("wallet_order_id"),
            purchase_token_hash=purchase_token_hash,
            verification_mode=verification_mode,
            acknowledgement_state=ack_state,
            consumption_state=consumption_state,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _raise_from_repo_error(exc)


def _decode_pubsub_notification(payload: GoogleNotificationIn) -> Tuple[Optional[str], Dict[str, Any]]:
    if payload.message:
        message_id = str(payload.message.messageId or payload.message.message_id or "").strip() or None
        data = str(payload.message.data or "").strip()
        if data:
            try:
                decoded = base64.b64decode(data).decode("utf-8")
                value = json.loads(decoded)
                if isinstance(value, dict):
                    return message_id, value
            except Exception:
                raise HTTPException(status_code=422, detail="invalid_google_pubsub_notification_data")
        return message_id, {}

    direct = payload.model_dump(exclude_none=True)
    direct.pop("message", None)
    direct.pop("subscription", None)
    return None, direct


def _notification_type(decoded: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    sub = decoded.get("subscriptionNotification")
    if isinstance(sub, dict):
        ntype = str(sub.get("notificationType") or "subscription").strip()
        return f"subscription:{ntype}", sub, str(sub.get("subscriptionId") or "").strip() or None, str(sub.get("purchaseToken") or "").strip() or None

    one = decoded.get("oneTimeProductNotification")
    if isinstance(one, dict):
        ntype = str(one.get("notificationType") or "one_time_product").strip()
        return f"one_time_product:{ntype}", one, str(one.get("sku") or "").strip() or None, str(one.get("purchaseToken") or "").strip() or None

    voided = decoded.get("voidedPurchaseNotification")
    if isinstance(voided, dict):
        return "voided_purchase", voided, None, str(voided.get("purchaseToken") or "").strip() or None

    test = decoded.get("testNotification")
    if isinstance(test, dict):
        return "test", test, None, None

    return "unknown", None, None, None


async def process_notification(
    conn,
    *,
    payload: GoogleNotificationIn,
) -> GoogleNotificationOut:
    require_google_play_iap_enabled()
    verification_mode = google_play_verification_mode()

    message_id, decoded = _decode_pubsub_notification(payload)
    package_name = normalize_package_name(decoded.get("packageName"))
    notification_type, notification_obj, google_product_id, purchase_token = _notification_type(decoded)
    purchase_token_hash = sha256_hex(purchase_token) if purchase_token else None
    message_key = message_id or f"{notification_type}:{package_name}:{google_product_id or ''}:{purchase_token_hash or ''}:{decoded.get('eventTimeMillis') or ''}"

    try:
        inserted = await record_google_notification_event(
            conn,
            message_id=message_key,
            notification_type=notification_type,
            package_name=package_name,
            google_product_id=google_product_id,
            purchase_token_hash=purchase_token_hash,
            decoded_payload_json=decoded,
            processing_status="processed",
        )

        if not inserted:
            return GoogleNotificationOut(
                message_id=message_key,
                notification_type=notification_type,
                google_product_id=google_product_id,
                purchase_token_hash=purchase_token_hash,
                processing_status="duplicate",
                verification_mode=verification_mode,
            )

        if notification_type.startswith("subscription:") and google_product_id and purchase_token and purchase_token_hash:
            verified = await fetch_subscription_from_google_api(package_name=package_name, purchase_token=purchase_token) if google_play_validate_purchases_enabled() else {}
            if not verified:
                verified = {
                    "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
                    "lineItems": [{"productId": google_product_id}],
                }

            state = derive_subscription_state(verified)
            resolved_user_id = await find_subscription_user_id(conn, purchase_token_hash=purchase_token_hash)
            if resolved_user_id is None:
                linked_hash = subscription_linked_token_hash(verified)
                if linked_hash:
                    resolved_user_id = await find_subscription_user_id(conn, purchase_token_hash=linked_hash)

            if resolved_user_id is not None:
                base_plan_id = subscription_base_plan_id(verified, None)
                region_code = subscription_region_code(verified, None)
                currency = settings.currency_for_country(region_code)
                start_time, end_time = subscription_period(verified)
                mapping = await resolve_google_product_mapping(
                    conn,
                    google_product_id=google_product_id,
                    expected_product_type="subscription",
                    currency=currency,
                    country_code=region_code,
                    base_plan_id=base_plan_id,
                )
                internal_plan_code = str(mapping.get("internal_plan_code") or "").strip()
                if internal_plan_code:
                    plan_profile = await fetch_plan_profile(conn, internal_plan_code=internal_plan_code)
                    mapping_md = as_dict_loose(mapping.get("metadata_json"))
                    tier_code = str(plan_profile.get("tier_code") or mapping_md.get("tier_code") or "").strip().lower()
                    interval_code = str(plan_profile.get("interval_code") or mapping_md.get("billing_interval") or "monthly").strip().lower()
                    grant_credits = int(plan_profile.get("monthly_grant_credits") or 0)
                    metadata_json = {
                        "provider": "google_play",
                        "notification_type": notification_type,
                        "message_id": message_key,
                        "google_product_id": google_product_id,
                        "base_plan_id": base_plan_id,
                        "purchase_token_hash": purchase_token_hash,
                        "verification_mode": verification_mode,
                        "package_name": package_name,
                        "country_code": region_code,
                        "currency": currency,
                    }
                    await upsert_google_subscription_row(
                        conn,
                        user_id=resolved_user_id,
                        purchase_token_hash=purchase_token_hash,
                        linked_purchase_token_hash=subscription_linked_token_hash(verified),
                        google_product_id=google_product_id,
                        base_plan_id=base_plan_id,
                        internal_plan_code=internal_plan_code,
                        subscription_state=str(state["subscription_state"]),
                        entitlement_state=str(state["entitlement_state"]),
                        current_period_start=start_time,
                        current_period_end=end_time,
                        cancel_at_period_end=bool(state["cancel_at_period_end"]),
                        canceled_at=datetime.now(timezone.utc) if str(state["subscription_state"]) == "canceled" else None,
                        trial_start=None,
                        trial_end=None,
                        metadata_json=metadata_json,
                    )

                    if bool(state.get("revert_to_free")):
                        cycle_key = google_cycle_key(
                            interval_code=interval_code,
                            period_start=start_time,
                            period_end=end_time,
                        )
                        await revert_user_to_free_entitlement(
                            conn,
                            user_id=resolved_user_id,
                            source="google_play_notification",
                            metadata_json=metadata_json,
                        )
                        await reconcile_included_plan_credits(
                            conn,
                            user_id=resolved_user_id,
                            plan_code="free",
                            tier_code="free",
                            included_credit_cap=None,
                            cycle_key=cycle_key,
                            current_period_start=start_time,
                            current_period_end=end_time,
                            source="google_play_notification_revert_to_free",
                            metadata_json=metadata_json,
                        )
                    elif str(state["entitlement_state"]) in {"active", "grace"}:
                        cycle_key = google_cycle_key(
                            interval_code=interval_code,
                            period_start=start_time,
                            period_end=end_time,
                        )
                        await apply_subscription_entitlement(
                            conn,
                            user_id=resolved_user_id,
                            tier_code=tier_code,
                            internal_plan_code=internal_plan_code,
                            cycle_key=cycle_key,
                            grant_credits=grant_credits,
                            current_period_start=start_time,
                            source="google_play_notification",
                            metadata_json=metadata_json,
                        )
                        await reconcile_included_plan_credits(
                            conn,
                            user_id=resolved_user_id,
                            plan_code=internal_plan_code,
                            tier_code=tier_code,
                            included_credit_cap=grant_credits,
                            cycle_key=cycle_key,
                            current_period_start=start_time,
                            current_period_end=end_time,
                            source="google_play_notification",
                            metadata_json=metadata_json,
                        )

        elif notification_type.startswith("one_time_product:") and google_product_id and purchase_token_hash:
            # RTDN does not include the user. If this purchase was already
            # confirmed by the app, it is already fulfilled idempotently. We
            # record the event here and leave unknown tokens untouched.
            resolved_user_id = await find_purchase_user_id(
                conn,
                package_name=package_name,
                google_product_id=google_product_id,
                purchase_token_hash=purchase_token_hash,
            )
            if resolved_user_id is not None and purchase_token:
                # Best effort: verify and consume again if the app confirm path
                # got interrupted after audit but before consume.
                await consume_product_best_effort(
                    package_name=package_name,
                    product_id=google_product_id,
                    purchase_token=purchase_token,
                )

        return GoogleNotificationOut(
            message_id=message_key,
            notification_type=notification_type,
            google_product_id=google_product_id,
            purchase_token_hash=purchase_token_hash,
            processing_status="processed",
            verification_mode=verification_mode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _raise_from_repo_error(exc)
