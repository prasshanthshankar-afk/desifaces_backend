
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, MutableMapping, Optional

from .models import (
    PricingCommitRequest,
    PricingCommitResponse,
    PricingPreviewRequest,
    PricingPreviewResponse,
    PricingReleaseRequest,
    PricingReleaseResponse,
    PricingReserveRequest,
    PricingReserveResponse,
)


def _as_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="python", exclude_none=False)
            if isinstance(dumped, dict):
                return dict(dumped)
        except Exception:
            pass
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _string_or_none(value: Any) -> Optional[str]:
    text = _clean_text(value)
    return text or None


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_int_string(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return str(int(Decimal(text)))
    except Exception:
        return text


def _money_str(value: Any) -> Optional[str]:
    dec = _decimal_or_none(value)
    if dec is None:
        return None
    return format(dec.quantize(Decimal("0.01")), "f")


def _display_money(currency: Optional[str], value: Any) -> Optional[str]:
    amount = _money_str(value)
    if amount is None:
        return None
    curr = _clean_text(currency, default="USD")
    return f"{curr} {amount}"


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _merge_meta(*parts: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for part in parts:
        if not part:
            continue
        merged.update(_as_dict(part))
    return merged


@dataclass(frozen=True)
class PricingPreviewSpec:
    user_id: str
    service_name: str
    service_action: str
    sku_code: str
    units: str
    idempotency_key: str
    meta: Dict[str, Any] = field(default_factory=dict)
    external_ref_type: str = "studio_job_preview"
    external_ref_id: Optional[str] = None


@dataclass(frozen=True)
class PricingReserveSpec:
    user_id: str
    service_name: str
    service_action: str
    sku_code: str
    units: str
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = field(default_factory=dict)
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None
    external_ref_type: str = "studio_job"


@dataclass(frozen=True)
class PricingCommitSpec:
    user_id: str
    reservation_id: str
    actual_units: str
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = field(default_factory=dict)
    external_ref_type: str = "studio_job"


@dataclass(frozen=True)
class PricingReleaseSpec:
    user_id: str
    reservation_id: str
    reason: str
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = field(default_factory=dict)
    external_ref_type: str = "studio_job"


def build_preview_request(spec: PricingPreviewSpec) -> PricingPreviewRequest:
    return PricingPreviewRequest(
        user_id=spec.user_id,
        service_name=spec.service_name,
        service_action=spec.service_action,
        sku_code=spec.sku_code,
        units=str(spec.units),
        external_ref_type=spec.external_ref_type,
        external_ref_id=spec.external_ref_id,
        idempotency_key=spec.idempotency_key,
        meta=dict(spec.meta),
    )


def build_reserve_request(spec: PricingReserveSpec) -> PricingReserveRequest:
    return PricingReserveRequest(
        user_id=spec.user_id,
        service_name=spec.service_name,
        service_action=spec.service_action,
        sku_code=spec.sku_code,
        units=str(spec.units),
        external_ref_type=spec.external_ref_type,
        external_ref_id=spec.external_ref_id,
        idempotency_key=spec.idempotency_key,
        quote_id=spec.quote_id,
        preview_fingerprint=spec.preview_fingerprint,
        meta=dict(spec.meta),
    )


def build_commit_request(spec: PricingCommitSpec) -> PricingCommitRequest:
    return PricingCommitRequest(
        user_id=spec.user_id,
        reservation_id=spec.reservation_id,
        actual_units=str(spec.actual_units),
        external_ref_type=spec.external_ref_type,
        external_ref_id=spec.external_ref_id,
        idempotency_key=spec.idempotency_key,
        meta=dict(spec.meta),
    )


def build_release_request(spec: PricingReleaseSpec) -> PricingReleaseRequest:
    return PricingReleaseRequest(
        user_id=spec.user_id,
        reservation_id=spec.reservation_id,
        reason=spec.reason,
        external_ref_type=spec.external_ref_type,
        external_ref_id=spec.external_ref_id,
        idempotency_key=spec.idempotency_key,
        meta=dict(spec.meta),
    )


def canonical_pricing_disabled(*, service_name: Optional[str] = None, service_action: Optional[str] = None) -> Dict[str, Any]:
    return {
        "state": "disabled",
        "quote_id": None,
        "quote_expires_at": None,
        "preview_fingerprint": None,
        "reservation_id": None,
        "service_name": service_name,
        "service_action": service_action,
        "sku_code": None,
        "unit_type": None,
        "estimated_units": None,
        "reserved_units": None,
        "actual_units": None,
        "billed_units": None,
        "released_units": None,
        "estimated_amount": None,
        "final_amount": None,
        "amount": None,
        "currency": "USD",
        "ledger_entry_id": None,
        "billing_mode": None,
        "billing_account_id": None,
        "settlement_mode": None,
        "pricing_mode": None,
        "entitlement_source": None,
        "entitlement_reason": None,
        "tier_code": None,
        "meta": {},
    }


def canonical_pricing_from_preview(
    response: PricingPreviewResponse | Mapping[str, Any],
    *,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
    sku_code: Optional[str] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    data = _as_dict(response)
    return {
        "state": _clean_text(data.get("status"), default="quoted"),
        "quote_id": _string_or_none(data.get("quote_id")),
        "quote_expires_at": _string_or_none(data.get("quote_expires_at")),
        "preview_fingerprint": _string_or_none(data.get("preview_fingerprint")),
        "reservation_id": None,
        "service_name": _string_or_none(_coalesce(data.get("service_name"), service_name)),
        "service_action": _string_or_none(_coalesce(data.get("service_action"), service_action)),
        "sku_code": _string_or_none(_coalesce(data.get("sku_code"), sku_code)),
        "unit_type": _string_or_none(data.get("unit_type")),
        "estimated_units": _as_int_string(data.get("estimated_units")),
        "reserved_units": None,
        "actual_units": None,
        "billed_units": None,
        "released_units": None,
        "estimated_amount": _money_str(data.get("estimated_amount")),
        "final_amount": None,
        "amount": _money_str(_coalesce(data.get("estimated_amount"), data.get("amount"))),
        "currency": _clean_text(data.get("currency"), default="USD"),
        "ledger_entry_id": None,
        "billing_mode": _string_or_none(data.get("billing_mode")),
        "billing_account_id": _string_or_none(data.get("billing_account_id")),
        "settlement_mode": _string_or_none(data.get("settlement_mode")),
        "pricing_mode": _string_or_none(_coalesce(data.get("pricing_mode"), data.get("billing_mode"))),
        "entitlement_source": _string_or_none(data.get("entitlement_source")),
        "entitlement_reason": _string_or_none(data.get("entitlement_reason")),
        "tier_code": _string_or_none(data.get("tier_code")),
        "meta": _merge_meta(meta, data.get("quote_breakdown"), data.get("summary")),
    }


def canonical_pricing_from_reserve(
    response: PricingReserveResponse | Mapping[str, Any],
    *,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
    sku_code: Optional[str] = None,
    estimated_units: Optional[str] = None,
    estimated_amount: Optional[str] = None,
    currency: Optional[str] = None,
    unit_type: Optional[str] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    data = _as_dict(response)
    effective_currency = _clean_text(_coalesce(data.get("currency"), currency), default="USD")
    effective_estimated_amount = _money_str(_coalesce(data.get("amount"), estimated_amount))
    return {
        "state": _clean_text(data.get("status"), default="reserved"),
        "quote_id": _string_or_none(data.get("quote_id")),
        "quote_expires_at": None,
        "preview_fingerprint": _string_or_none(data.get("preview_fingerprint")),
        "reservation_id": _string_or_none(data.get("reservation_id")),
        "service_name": _string_or_none(service_name),
        "service_action": _string_or_none(service_action),
        "sku_code": _string_or_none(_coalesce(sku_code, data.get("sku_code"))),
        "unit_type": _string_or_none(unit_type),
        "estimated_units": _as_int_string(estimated_units),
        "reserved_units": _as_int_string(data.get("reserved_units")),
        "actual_units": None,
        "billed_units": _as_int_string(data.get("billed_units")),
        "released_units": None,
        "estimated_amount": effective_estimated_amount,
        "final_amount": None,
        "amount": effective_estimated_amount,
        "currency": effective_currency,
        "ledger_entry_id": _string_or_none(data.get("ledger_entry_id")),
        "billing_mode": _string_or_none(data.get("billing_mode")),
        "billing_account_id": _string_or_none(data.get("billing_account_id")),
        "settlement_mode": _string_or_none(data.get("settlement_mode")),
        "pricing_mode": _string_or_none(_coalesce(data.get("pricing_mode"), data.get("billing_mode"))),
        "entitlement_source": _string_or_none(data.get("entitlement_source")),
        "entitlement_reason": _string_or_none(data.get("entitlement_reason")),
        "tier_code": _string_or_none(data.get("tier_code")),
        "meta": _merge_meta(meta, data.get("meta")),
    }


def canonical_pricing_from_commit(
    response: PricingCommitResponse | Mapping[str, Any],
    *,
    base_pricing: Optional[Mapping[str, Any]] = None,
    actual_units: Optional[str] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    prev = _as_dict(base_pricing)
    data = _as_dict(response)
    effective_currency = _clean_text(_coalesce(data.get("currency"), prev.get("currency")), default="USD")
    final_amount = _money_str(_coalesce(data.get("amount"), prev.get("final_amount"), prev.get("amount")))
    return {
        "state": _clean_text(data.get("status"), default="committed"),
        "quote_id": _string_or_none(prev.get("quote_id")),
        "quote_expires_at": None,
        "preview_fingerprint": _string_or_none(prev.get("preview_fingerprint")),
        "reservation_id": _string_or_none(_coalesce(data.get("reservation_id"), prev.get("reservation_id"))),
        "service_name": _string_or_none(prev.get("service_name")),
        "service_action": _string_or_none(prev.get("service_action")),
        "sku_code": _string_or_none(prev.get("sku_code")),
        "unit_type": _string_or_none(prev.get("unit_type")),
        "estimated_units": _as_int_string(prev.get("estimated_units")),
        "reserved_units": _as_int_string(prev.get("reserved_units")),
        "actual_units": _as_int_string(_coalesce(actual_units, prev.get("actual_units"))),
        "billed_units": _as_int_string(_coalesce(data.get("billed_units"), actual_units, prev.get("billed_units"), prev.get("actual_units"))),
        "released_units": None,
        "estimated_amount": _money_str(prev.get("estimated_amount")),
        "final_amount": final_amount,
        "amount": final_amount,
        "currency": effective_currency,
        "ledger_entry_id": _string_or_none(data.get("ledger_entry_id")),
        "billing_mode": _string_or_none(_coalesce(data.get("billing_mode"), prev.get("billing_mode"))),
        "billing_account_id": _string_or_none(_coalesce(data.get("billing_account_id"), prev.get("billing_account_id"))),
        "settlement_mode": _string_or_none(_coalesce(data.get("settlement_mode"), prev.get("settlement_mode"))),
        "pricing_mode": _string_or_none(_coalesce(prev.get("pricing_mode"), data.get("billing_mode"), prev.get("billing_mode"))),
        "entitlement_source": _string_or_none(_coalesce(data.get("entitlement_source"), prev.get("entitlement_source"))),
        "entitlement_reason": _string_or_none(_coalesce(data.get("entitlement_reason"), prev.get("entitlement_reason"))),
        "tier_code": _string_or_none(_coalesce(data.get("tier_code"), prev.get("tier_code"))),
        "meta": _merge_meta(prev.get("meta"), meta, data.get("meta")),
    }


def canonical_pricing_from_release(
    response: PricingReleaseResponse | Mapping[str, Any],
    *,
    base_pricing: Optional[Mapping[str, Any]] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    prev = _as_dict(base_pricing)
    data = _as_dict(response)
    return {
        "state": _clean_text(data.get("status"), default="released"),
        "quote_id": _string_or_none(prev.get("quote_id")),
        "quote_expires_at": None,
        "preview_fingerprint": _string_or_none(prev.get("preview_fingerprint")),
        "reservation_id": _string_or_none(_coalesce(data.get("reservation_id"), prev.get("reservation_id"))),
        "service_name": _string_or_none(prev.get("service_name")),
        "service_action": _string_or_none(prev.get("service_action")),
        "sku_code": _string_or_none(prev.get("sku_code")),
        "unit_type": _string_or_none(prev.get("unit_type")),
        "estimated_units": _as_int_string(prev.get("estimated_units")),
        "reserved_units": _as_int_string(prev.get("reserved_units")),
        "actual_units": _as_int_string(prev.get("actual_units")),
        "billed_units": _as_int_string(prev.get("billed_units")),
        "released_units": _as_int_string(_coalesce(data.get("released_units"), prev.get("reserved_units"), prev.get("estimated_units"))),
        "estimated_amount": _money_str(prev.get("estimated_amount")),
        "final_amount": _money_str("0"),
        "amount": _money_str("0"),
        "currency": _clean_text(prev.get("currency"), default="USD"),
        "ledger_entry_id": None,
        "billing_mode": _string_or_none(_coalesce(data.get("billing_mode"), prev.get("billing_mode"))),
        "billing_account_id": _string_or_none(_coalesce(data.get("billing_account_id"), prev.get("billing_account_id"))),
        "settlement_mode": _string_or_none(_coalesce(data.get("settlement_mode"), prev.get("settlement_mode"))),
        "pricing_mode": _string_or_none(prev.get("pricing_mode")),
        "entitlement_source": _string_or_none(_coalesce(data.get("entitlement_source"), prev.get("entitlement_source"))),
        "entitlement_reason": _string_or_none(_coalesce(data.get("entitlement_reason"), prev.get("entitlement_reason"))),
        "tier_code": _string_or_none(prev.get("tier_code")),
        "meta": _merge_meta(prev.get("meta"), meta, data.get("meta"), {"release_reason": data.get("message")}),
    }


def build_pricing_summary(pricing: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    p = _as_dict(pricing)
    currency = _clean_text(p.get("currency"), default="USD")
    estimate = _decimal_or_none(p.get("estimated_amount"))
    final = _decimal_or_none(_coalesce(p.get("final_amount"), p.get("amount")))
    state = _clean_text(p.get("state"))

    summary: Dict[str, Any] = {
        "display_estimate": _display_money(currency, estimate),
        "display_final": _display_money(currency, final),
        "display_delta": None,
        "display_note": None,
    }

    if estimate is not None and final is not None:
        summary["display_delta"] = _display_money(currency, final - estimate)

    if state == "quoted":
        summary["display_note"] = "Estimated price before execution."
    elif state == "reserved":
        summary["display_note"] = "Estimated charge reserved before execution."
    elif state == "committed":
        summary["display_note"] = "Final charge recorded after execution."
    elif state == "released":
        summary["display_note"] = "Reservation released; no final charge recorded."
    elif state == "disabled":
        summary["display_note"] = "Pricing disabled for this environment."

    return summary


def apply_pricing_snapshot(
    target: MutableMapping[str, Any],
    *,
    pricing: Optional[Mapping[str, Any]] = None,
    pricing_summary: Optional[Mapping[str, Any]] = None,
) -> MutableMapping[str, Any]:
    canonical = _as_dict(pricing)
    summary = _as_dict(pricing_summary) if pricing_summary else build_pricing_summary(canonical)
    target["pricing"] = canonical
    target["pricing_summary"] = summary
    return target


def make_preview_artifact(
    response: PricingPreviewResponse | Mapping[str, Any],
    *,
    service_name: str,
    service_action: str,
    sku_code: str,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    pricing = canonical_pricing_from_preview(
        response,
        service_name=service_name,
        service_action=service_action,
        sku_code=sku_code,
        meta=meta,
    )
    return {"pricing": pricing, "pricing_summary": build_pricing_summary(pricing)}


def make_reserved_artifact(
    response: PricingReserveResponse | Mapping[str, Any],
    *,
    service_name: str,
    service_action: str,
    sku_code: str,
    estimated_units: Optional[str] = None,
    estimated_amount: Optional[str] = None,
    currency: Optional[str] = None,
    unit_type: Optional[str] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    pricing = canonical_pricing_from_reserve(
        response,
        service_name=service_name,
        service_action=service_action,
        sku_code=sku_code,
        estimated_units=estimated_units,
        estimated_amount=estimated_amount,
        currency=currency,
        unit_type=unit_type,
        meta=meta,
    )
    return {"pricing": pricing, "pricing_summary": build_pricing_summary(pricing)}


def make_committed_artifact(
    response: PricingCommitResponse | Mapping[str, Any],
    *,
    base_pricing: Optional[Mapping[str, Any]] = None,
    actual_units: Optional[str] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    pricing = canonical_pricing_from_commit(
        response,
        base_pricing=base_pricing,
        actual_units=actual_units,
        meta=meta,
    )
    return {"pricing": pricing, "pricing_summary": build_pricing_summary(pricing)}


def make_released_artifact(
    response: PricingReleaseResponse | Mapping[str, Any],
    *,
    base_pricing: Optional[Mapping[str, Any]] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    pricing = canonical_pricing_from_release(
        response,
        base_pricing=base_pricing,
        meta=meta,
    )
    return {"pricing": pricing, "pricing_summary": build_pricing_summary(pricing)}


def pricing_confirmation_from_preview(
    response: PricingPreviewResponse | Mapping[str, Any],
) -> Dict[str, Optional[str]]:
    data = _as_dict(response)
    return {
        "quote_id": _string_or_none(data.get("quote_id")),
        "preview_fingerprint": _string_or_none(data.get("preview_fingerprint")),
    }


def merge_confirmation_into_meta(
    meta: Optional[Mapping[str, Any]],
    preview_response: PricingPreviewResponse | Mapping[str, Any],
) -> Dict[str, Any]:
    merged = _merge_meta(meta)
    merged["pricing_confirmation"] = pricing_confirmation_from_preview(preview_response)
    return merged
