"""Pricing compatibility bridge for desifaces-v3.

This module translates the current pricing preview/reservation lifecycle into
canonical V3 PricingQuote, CreditReservation, and CreditTransaction contracts.
It is pure: no FastAPI, database, payment-provider, or svc-pricing imports.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import Field

from .common import V3ContractModel
from .commerce import (
    CreditEntryType,
    CreditReservation,
    CreditReservationState,
    CreditTransaction,
    PriceMoney,
    PricingQuote,
)


class PricingQuoteBridgeResult(V3ContractModel):
    quote: PricingQuote
    legacy_quote_id: str | None = None
    compatibility_metadata: dict[str, Any] = Field(default_factory=dict)


class PricingReservationBridgeResult(V3ContractModel):
    reservation: CreditReservation
    legacy_quote_id: str | None = None
    compatibility_metadata: dict[str, Any] = Field(default_factory=dict)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except Exception:
            return {}
    return {}


def _as_uuid(value: Any) -> UUID | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return UUID(text)
    except Exception:
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(Decimal(str(value)))
    except Exception:
        return default


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _stable_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _money_minor(amount: Any, currency: str) -> PriceMoney | None:
    text = _clean(amount)
    if text is None:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if value < 0:
        return None
    minor = int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return PriceMoney(currency=currency.upper(), amount_minor=minor)


def canonical_quote_id(*, account_id: UUID, user_id: UUID, fingerprint: str) -> UUID:
    """Issue a stable V3 quote UUID for a legacy preview fingerprint.

    The pricing bridge is the authoritative place where a non-UUID legacy quote
    becomes a canonical quote identity. Capability adapters must never invent one.
    """

    return uuid5(NAMESPACE_URL, f"desifaces:v3:pricing:{account_id}:{user_id}:{fingerprint}")


def adapt_pricing_preview_response(
    payload: Mapping[str, Any],
    *,
    account_id: UUID,
    user_id: UUID,
    operation: str | None = None,
    created_at: datetime | None = None,
) -> PricingQuoteBridgeResult:
    """Translate current reservations-preview output into canonical PricingQuote."""

    data = dict(payload)
    breakdown = _mapping(data.get("quote_breakdown"))

    fingerprint = (
        _clean(data.get("preview_fingerprint"))
        or _clean(breakdown.get("preview_fingerprint"))
        or _stable_fingerprint(
            {
                "service_name": data.get("service_name"),
                "service_action": data.get("service_action"),
                "sku_code": data.get("sku_code"),
                "quote_breakdown": breakdown,
            }
        )
    )

    legacy_quote_id = _clean(data.get("quote_id")) or _clean(breakdown.get("quote_id"))
    current_uuid = _as_uuid(legacy_quote_id)
    quote_id = current_uuid or canonical_quote_id(
        account_id=account_id,
        user_id=user_id,
        fingerprint=fingerprint,
    )

    effective_operation = (
        _clean(operation)
        or _clean(data.get("service_action"))
        or _clean(breakdown.get("service_action"))
        or _clean(data.get("sku_code"))
        or _clean(breakdown.get("sku_code"))
        or "pricing.quote"
    )

    credits = _as_int(
        breakdown.get("total_credits")
        if breakdown.get("total_credits") is not None
        else data.get("estimated_credits"),
        0,
    )
    currency = (
        _clean(data.get("currency"))
        or _clean(breakdown.get("currency"))
        or "USD"
    ).upper()
    amount = (
        data.get("estimated_amount")
        if data.get("estimated_amount") is not None
        else breakdown.get("total_money")
    )

    pricebook_revision = (
        _clean(breakdown.get("pricebook_revision"))
        or _clean(breakdown.get("pricebook_id"))
        or _clean(breakdown.get("pricebook_name"))
        or "legacy-current"
    )

    now = created_at or datetime.now(timezone.utc)
    expires_at = (
        _as_datetime(data.get("quote_expires_at"))
        or _as_datetime(breakdown.get("quote_expires_at"))
        or now + timedelta(minutes=15)
    )

    quote = PricingQuote(
        quote_id=quote_id,
        account_id=account_id,
        user_id=user_id,
        operation=effective_operation,
        credits=max(0, credits),
        money=_money_minor(amount, currency),
        pricebook_revision=pricebook_revision,
        fingerprint=fingerprint,
        expires_at=expires_at,
        created_at=now,
    )

    compatibility_metadata: dict[str, Any] = {
        "billing_mode": data.get("billing_mode"),
        "pricing_mode": data.get("pricing_mode"),
        "settlement_mode": data.get("settlement_mode"),
        "entitlement_source": data.get("entitlement_source"),
        "entitlement_reason": data.get("entitlement_reason"),
        "tier_code": data.get("tier_code"),
        "summary": _mapping(data.get("summary")),
    }
    compatibility_metadata = {
        key: value for key, value in compatibility_metadata.items() if value not in (None, "", {})
    }
    if legacy_quote_id and current_uuid is None:
        compatibility_metadata["legacy_quote_id"] = legacy_quote_id

    return PricingQuoteBridgeResult(
        quote=quote,
        legacy_quote_id=legacy_quote_id,
        compatibility_metadata=compatibility_metadata,
    )


def adapt_pricing_reserve_response(
    payload: Mapping[str, Any],
    *,
    quote: PricingQuote,
    account_id: UUID,
    user_id: UUID,
    reference_type: str | None = None,
    reference_id: str | None = None,
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> PricingReservationBridgeResult:
    """Translate current reserve output into canonical CreditReservation."""

    data = dict(payload)
    reservation_id = _as_uuid(data.get("reservation_id"))
    if reservation_id is None:
        raise ValueError("pricing reservation_id must be a UUID for canonical V3 mapping")

    now = created_at or datetime.now(timezone.utc)
    state_text = (_clean(data.get("status")) or "reserved").lower()
    state_map = {
        "reserved": CreditReservationState.RESERVED,
        "committed": CreditReservationState.COMMITTED,
        "finalized": CreditReservationState.COMMITTED,
        "released": CreditReservationState.RELEASED,
        "expired": CreditReservationState.EXPIRED,
    }
    state = state_map.get(state_text, CreditReservationState.RESERVED)

    reserved_credits = _as_int(
        data.get("reserved_credits")
        if data.get("reserved_credits") is not None
        else data.get("estimated_credits"),
        quote.credits,
    )
    if reserved_credits == 0 and quote.credits > 0:
        reserved_credits = quote.credits

    reservation = CreditReservation(
        reservation_id=reservation_id,
        account_id=account_id,
        user_id=user_id,
        quote_id=quote.quote_id,
        state=state,
        reserved_credits=max(0, reserved_credits),
        reference_type=_clean(reference_type),
        reference_id=_clean(reference_id),
        idempotency_key=_clean(idempotency_key),
        expires_at=_as_datetime(data.get("expires_at")),
        created_at=now,
        updated_at=now,
    )

    legacy_quote_id = _clean(data.get("quote_id"))
    compatibility_metadata = {
        "preview_fingerprint": _clean(data.get("preview_fingerprint")),
        "reserved_units": _clean(data.get("reserved_units")),
        "billing_mode": _clean(data.get("billing_mode")),
        "pricing_mode": _clean(data.get("pricing_mode")),
        "settlement_mode": _clean(data.get("settlement_mode")),
    }
    compatibility_metadata = {
        key: value for key, value in compatibility_metadata.items() if value not in (None, "")
    }

    return PricingReservationBridgeResult(
        reservation=reservation,
        legacy_quote_id=legacy_quote_id,
        compatibility_metadata=compatibility_metadata,
    )


def credit_transaction_from_commit(
    *,
    account_id: UUID,
    user_id: UUID,
    reservation_id: UUID,
    charged_credits: int,
    balance_after: int,
    idempotency_key: str,
    ledger_entry_id: UUID | None = None,
    created_at: datetime | None = None,
) -> CreditTransaction:
    """Map a finalized prepaid charge to an immutable canonical transaction.

    ``charged_credits`` and ``balance_after`` must come from settlement/ledger
    evidence, not from service units in the public commit response.
    """

    if charged_credits < 0:
        raise ValueError("charged_credits must be non-negative")
    if balance_after < 0:
        raise ValueError("balance_after must be non-negative")

    values: dict[str, Any] = {
        "account_id": account_id,
        "user_id": user_id,
        "entry_type": CreditEntryType.CONSUMPTION,
        "credits_delta": -charged_credits,
        "balance_after": balance_after,
        "reference_type": "credit_reservation",
        "reference_id": str(reservation_id),
        "idempotency_key": idempotency_key,
        "created_at": created_at or datetime.now(timezone.utc),
    }
    if ledger_entry_id is not None:
        values["transaction_id"] = ledger_entry_id
    return CreditTransaction(**values)


__all__ = [
    "PricingQuoteBridgeResult",
    "PricingReservationBridgeResult",
    "adapt_pricing_preview_response",
    "adapt_pricing_reserve_response",
    "canonical_quote_id",
    "credit_transaction_from_commit",
]
