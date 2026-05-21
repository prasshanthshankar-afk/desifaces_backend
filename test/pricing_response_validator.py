
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_ALLOWED_STATES = {
    "quoted",
    "estimated",
    "pending_reservation",
    "reserved",
    "processing",
    "running",
    "finalizing",
    "committed",
    "released",
    "reservation_failed",
    "commit_failed",
    "release_failed",
    "failed",
    "canceled",
}


@dataclass(frozen=True)
class PricingValidationResult:
    ok: bool
    errors: List[str]
    normalized: Dict[str, Any]


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _get_top_level_pricing(payload: Dict[str, Any]) -> Dict[str, Any]:
    pricing = _as_dict(payload.get("pricing"))
    if pricing:
        return pricing

    # common fallback shapes
    job = _as_dict(payload.get("job"))
    pricing = _as_dict(job.get("pricing"))
    if pricing:
        return pricing

    data = _as_dict(payload.get("data"))
    pricing = _as_dict(data.get("pricing"))
    if pricing:
        return pricing

    return {}


def _get_top_level_pricing_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary = _as_dict(payload.get("pricing_summary"))
    if summary:
        return summary
    job = _as_dict(payload.get("job"))
    summary = _as_dict(job.get("pricing_summary"))
    if summary:
        return summary
    data = _as_dict(payload.get("data"))
    summary = _as_dict(data.get("pricing_summary"))
    if summary:
        return summary
    return {}


def _maybe_job_id(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("job_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    job = _as_dict(payload.get("job"))
    for key in ("job_id", "id"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def validate_pricing_contract(
    payload: Dict[str, Any],
    *,
    required: bool = True,
    allowed_states: Optional[Iterable[str]] = None,
    require_summary: bool = True,
) -> PricingValidationResult:
    errors: List[str] = []
    pricing = _get_top_level_pricing(payload)
    summary = _get_top_level_pricing_summary(payload)
    states = set(allowed_states or DEFAULT_ALLOWED_STATES)

    if required and not pricing:
        errors.append("Missing top-level pricing object.")
    if require_summary and not summary:
        errors.append("Missing top-level pricing_summary object.")

    if pricing:
        if "state" not in pricing:
            errors.append("pricing.state is missing.")
        else:
            state = pricing.get("state")
            if isinstance(state, str):
                if state not in states:
                    errors.append(f"Unexpected pricing.state={state!r}.")
            else:
                errors.append("pricing.state must be a string.")

        required_keys = (
            "enabled",
            "billing_mode",
            "tier_code",
        )
        for key in required_keys:
            if key not in pricing:
                errors.append(f"pricing.{key} is missing.")

        if "pricing_mode" not in pricing and "source" not in pricing:
            errors.append("pricing should include at least pricing_mode or source.")

    if summary:
        if "state" not in summary:
            errors.append("pricing_summary.state is missing.")
        elif pricing and summary.get("state") != pricing.get("state"):
            errors.append(
                f"pricing_summary.state={summary.get('state')!r} does not match pricing.state={pricing.get('state')!r}."
            )

        summary_tier = summary.get("tier_code")
        pricing_tier = pricing.get("tier_code") if pricing else None
        if summary_tier is not None and pricing_tier is not None and summary_tier != pricing_tier:
            errors.append(
                f"pricing_summary.tier_code={summary_tier!r} does not match pricing.tier_code={pricing_tier!r}."
            )

    normalized = {
        "job_id": _maybe_job_id(payload),
        "pricing": pricing,
        "pricing_summary": summary,
    }
    return PricingValidationResult(ok=not errors, errors=errors, normalized=normalized)


def assert_pricing_contract(
    payload: Dict[str, Any],
    *,
    required: bool = True,
    allowed_states: Optional[Iterable[str]] = None,
    require_summary: bool = True,
) -> Dict[str, Any]:
    result = validate_pricing_contract(
        payload,
        required=required,
        allowed_states=allowed_states,
        require_summary=require_summary,
    )
    if not result.ok:
        joined = "\n - ".join(result.errors)
        raise AssertionError(f"Pricing contract validation failed:\n - {joined}")
    return result.normalized


def summarize_pricing(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = assert_pricing_contract(payload, required=True, require_summary=False)
    pricing = normalized["pricing"]
    summary = normalized["pricing_summary"]
    return {
        "job_id": normalized["job_id"],
        "state": pricing.get("state"),
        "billing_mode": pricing.get("billing_mode"),
        "settlement_mode": pricing.get("settlement_mode"),
        "tier_code": pricing.get("tier_code"),
        "quote_id": pricing.get("quote_id"),
        "reservation_id": pricing.get("reservation_id"),
        "variant_code": pricing.get("variant_code"),
        "sku_code": pricing.get("sku_code"),
        "estimated_units": pricing.get("estimated_units"),
        "actual_units": pricing.get("actual_units"),
        "billed_units": pricing.get("billed_units"),
        "final_amount": pricing.get("final_amount"),
        "currency": pricing.get("currency"),
        "ledger_entry_id": pricing.get("ledger_entry_id"),
        "source": pricing.get("source"),
        "reason": pricing.get("reason"),
        "summary": summary,
    }
