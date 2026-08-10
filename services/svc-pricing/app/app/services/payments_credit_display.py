from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, Optional


def _as_dict_deep_loose(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return dict(x)
    if isinstance(x, (list, tuple)):
        merged: Dict[str, Any] = {}
        for item in x:
            item_dict = _as_dict_deep_loose(item)
            if item_dict:
                merged.update(item_dict)
        return merged
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            decoded = json.loads(s)
        except Exception:
            return {}
        return _as_dict_deep_loose(decoded)
    try:
        return dict(x)
    except Exception:
        return {}


def _to_decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_int_credits(value: Any, default: int = 0) -> int:
    d = _to_decimal_or_none(value)
    if d is None:
        return default
    try:
        return max(int(d.to_integral_value(rounding="ROUND_HALF_UP")), 0)
    except Exception:
        return default


def _first_decimal(*values: Any) -> Optional[Decimal]:
    for value in values:
        d = _to_decimal_or_none(value)
        if d is not None:
            return d
    return None


def _credit_label(value: Any) -> str:
    return f"{_to_int_credits(value, 0)} credits"


def _included_label(available: int, total: int) -> str:
    return f"{max(int(available), 0)} / {max(int(total), 0)} credits"


def _cycle_usage_label(used: int, total: int) -> str:
    if total > 0:
        return f"{max(int(used), 0)} / {int(total)} credits used"
    return f"{max(int(used), 0)} credits used"


def _tier_from_plan_code(plan_code: Optional[str]) -> str:
    code = str(plan_code or "").strip().lower()
    if code.startswith("enterprise"):
        return "enterprise"
    if code.startswith("business") or code.startswith("team"):
        return "business"
    if code.startswith("pro"):
        return "pro"
    return "free"


def _plan_code_from_tier(tier_code: Optional[str]) -> str:
    tier = str(tier_code or "").strip().lower()
    if tier == "enterprise":
        return "enterprise_monthly_v1"
    if tier == "business":
        return "business_monthly_v1"
    if tier == "pro":
        return "pro_monthly_v1"
    return "free"


def _default_plan_name(plan_code: str) -> str:
    code = str(plan_code or "").strip().lower()
    if code == "free":
        return "Free"
    if "enterprise" in code:
        return "Enterprise Yearly" if "yearly" in code else "Enterprise"
    if "business" in code:
        return "Business Yearly" if "yearly" in code else "Business"
    if "pro" in code:
        return "Pro Yearly" if "yearly" in code else "Pro"
    return code.replace("_", " ").title() or "Plan"


def _public_plan_name(plan_code: Optional[str]) -> str:
    code = str(plan_code or "").strip().lower()
    if not code:
        return ""
    if code == "free":
        return "Free"
    if code in {"pro_monthly_v1", "pro_monthly", "pro"}:
        return "Pro Monthly"
    if code == "pro_yearly_v1":
        return "Pro Yearly"
    if code in {"business_monthly_v1", "business_monthly", "business"}:
        return "Business Monthly"
    if code == "business_yearly_v1":
        return "Business Yearly"
    if code in {"enterprise_monthly_v1", "enterprise_monthly"}:
        return "Enterprise Monthly"
    if code in {"enterprise_yearly_v1", "enterprise_contract_v1", "enterprise"}:
        return "Enterprise"
    return _default_plan_name(code)


def _canonical_plan_name(plan_code: Optional[str], tier_code: Optional[str] = None) -> str:
    code = str(plan_code or "").strip()
    if code:
        return _public_plan_name(code) or _default_plan_name(code)
    tier = str(tier_code or "").strip().lower()
    if tier:
        return _default_plan_name(_plan_code_from_tier(tier))
    return "Free"


def _bucket_validity_label(bucket: str, expires_at: Any) -> str:
    if expires_at:
        return f"Valid until {expires_at}"
    if bucket == "included":
        return "Valid until the current billing period renews"
    if bucket == "purchased":
        return "No expiry"
    if bucket == "promo":
        return "Show promo expiry if present"
    return ""


def build_canonical_billing_display(
    *,
    overview: Dict[str, Any],
    current_ent: Any,
    credit_account: Any,
    pricing_account_overview: Any,
    current_plan_code: str,
    current_tier_code: str,
) -> Dict[str, Any]:
    """Apply the customer-facing credit display contract.

    billing_entitlements is plan metadata only. Live spendable balance comes
    from v_pricing_account_overview lots/legacy account or credit account
    fallback. Bucket meanings are returned for UI clarity.
    """
    ent = _as_dict_deep_loose(current_ent)
    overview_credits = _as_dict_deep_loose(overview.get("credits"))
    overview_header = _as_dict_deep_loose(overview.get("header"))
    pao = _as_dict_deep_loose(pricing_account_overview)
    plan_json = _as_dict_deep_loose(pao.get("plan_json"))
    lots_json = _as_dict_deep_loose(pao.get("lots_json"))
    legacy_account = _as_dict_deep_loose(pao.get("legacy_account_json"))
    account = _as_dict_deep_loose(credit_account)

    normalized_plan_code = str(
        current_plan_code
        or ent.get("plan_code")
        or plan_json.get("plan_code")
        or overview.get("plan_code")
        or "free"
    ).strip().lower() or "free"
    tier_code = str(
        current_tier_code
        or ent.get("tier_code")
        or plan_json.get("tier_code")
        or _tier_from_plan_code(normalized_plan_code)
    ).strip().lower() or "free"

    billing_mode = str(
        ent.get("billing_mode")
        or plan_json.get("billing_mode")
        or overview.get("billing_mode")
        or ""
    ).strip().lower()
    settlement_mode = str(
        ent.get("settlement_mode")
        or plan_json.get("settlement_mode")
        or overview.get("settlement_mode")
        or ""
    ).strip().lower()

    plan_total_decimal = _first_decimal(
        plan_json.get("included_credits_total"),
        ent.get("included_credits_total"),
        overview_credits.get("total_credits"),
    )
    plan_total = _to_int_credits(plan_total_decimal, 0)

    included_available = _to_int_credits(
        _first_decimal(lots_json.get("included_available"), lots_json.get("included_credits_available")),
        default=-1,
    )
    if included_available < 0:
        included_available = _to_int_credits(
            _first_decimal(
                overview_credits.get("included_available"),
                overview_credits.get("available_credits"),
                legacy_account.get("legacy_balance_credits"),
                account.get("balance_credits"),
            ),
            0,
        )

    included_reserved = _to_int_credits(
        _first_decimal(lots_json.get("included_reserved"), lots_json.get("included_credits_reserved")),
        0,
    )
    wallet_available = _to_int_credits(
        _first_decimal(lots_json.get("purchased_available"), lots_json.get("wallet_available"), lots_json.get("topup_available")),
        0,
    )
    wallet_reserved = _to_int_credits(
        _first_decimal(lots_json.get("purchased_reserved"), lots_json.get("wallet_reserved"), lots_json.get("topup_reserved")),
        0,
    )
    promo_available = _to_int_credits(_first_decimal(lots_json.get("promo_available")), 0)
    promo_reserved = _to_int_credits(_first_decimal(lots_json.get("promo_reserved")), 0)

    total_available = _to_int_credits(
        _first_decimal(
            lots_json.get("total_spendable"),
            lots_json.get("total_available"),
            overview_credits.get("total_available"),
            overview_credits.get("available_credits"),
            legacy_account.get("legacy_balance_credits"),
            account.get("balance_credits"),
        ),
        included_available + wallet_available + promo_available,
    )
    total_reserved = _to_int_credits(
        _first_decimal(
            lots_json.get("total_reserved"),
            overview_credits.get("total_reserved"),
            overview_credits.get("reserved_credits"),
            legacy_account.get("legacy_reserved_credits"),
            account.get("reserved_credits"),
        ),
        included_reserved + wallet_reserved + promo_reserved,
    )

    # Do not infer usage from unavailable plan credits. Expired/stale included
    # credits are not customer usage. Usage must come from the canonical lots
    # read model, which computes actual committed included-credit spend.
    included_used = _to_int_credits(
        _first_decimal(lots_json.get("included_used")),
        0,
    )

    usage_percent: Optional[float] = None
    if plan_total > 0:
        usage_percent = round(min(max(included_used / plan_total, 0.0), 1.0) * 100.0, 2)

    is_enterprise = tier_code == "enterprise" or normalized_plan_code.startswith("enterprise")
    is_postpaid = settlement_mode in {"postpaid", "money", "invoice"} or billing_mode in {"postpaid", "invoice"}

    billing_model = "postpaid" if (is_enterprise or is_postpaid) else "prepaid"
    plan_name = _canonical_plan_name(normalized_plan_code, tier_code)

    if billing_model == "postpaid":
        display = {
            "header_label": f"{plan_name} • Postpaid" if plan_name else "Enterprise • Postpaid",
            "billing_label": "Monthly usage in credits",
            "included_label": "Postpaid credits",
            "wallet_label": "Not needed",
            "reserved_label": _credit_label(total_reserved),
            "cycle_usage_label": _cycle_usage_label(included_used, plan_total),
            "total_available_label": "Postpaid credits",
        }
    else:
        display = {
            "header_label": f"{total_available} available • {total_reserved} reserved",
            "billing_label": "Credits",
            "included_label": _included_label(included_available, plan_total),
            "wallet_label": _credit_label(wallet_available),
            "reserved_label": _credit_label(total_reserved),
            "cycle_usage_label": _cycle_usage_label(included_used, plan_total),
            "total_available_label": _credit_label(total_available),
        }

    canonical_plan = {
        "plan_code": normalized_plan_code,
        "plan_name": plan_name,
        "tier_code": tier_code,
        "billing_mode": billing_mode or ("invoice" if billing_model == "postpaid" else "subscription"),
        "settlement_mode": settlement_mode or ("postpaid" if billing_model == "postpaid" else "credits"),
        "included_credits_total": plan_total,
        "source": "billing_entitlements+pricing_account_overview",
    }

    included_expires_at = lots_json.get("included_expires_at") or lots_json.get("included_valid_until") or ent.get("effective_to")
    purchased_expires_at = lots_json.get("purchased_expires_at") or lots_json.get("wallet_expires_at")
    promo_expires_at = lots_json.get("promo_expires_at")

    breakdown = {
        "included": {
            "key": "included",
            "label": f"{plan_name} plan credits" if plan_name else "Subscription plan credits",
            "meaning": "Credits included with your current subscription plan.",
            "available": included_available,
            "reserved": included_reserved,
            "used": included_used,
            "total": plan_total,
            "expires_at": included_expires_at,
            "validity_label": _bucket_validity_label("included", included_expires_at),
        },
        "purchased": {
            "key": "purchased",
            "label": "Wallet / carried-over credits",
            "meaning": "Top-up, migrated, or carried-over credits that are additive on top of plan credits.",
            "available": wallet_available,
            "reserved": wallet_reserved,
            "used": 0,
            "total": wallet_available + wallet_reserved,
            "expires_at": purchased_expires_at,
            "validity_label": _bucket_validity_label("purchased", purchased_expires_at),
        },
        "promo": {
            "key": "promo",
            "label": "Promo credits",
            "meaning": "Promotional or admin-granted credits.",
            "available": promo_available,
            "reserved": promo_reserved,
            "used": 0,
            "total": promo_available + promo_reserved,
            "expires_at": promo_expires_at,
            "validity_label": _bucket_validity_label("promo", promo_expires_at),
        },
    }

    canonical_credits = {
        "available_credits": total_available,
        "reserved_credits": total_reserved,
        "used_credits": included_used,
        "total_credits": plan_total,
        "included_available": included_available,
        "included_reserved": included_reserved,
        "included_used": included_used,
        "wallet_available": wallet_available,
        "wallet_reserved": wallet_reserved,
        "promo_available": promo_available,
        "promo_reserved": promo_reserved,
        "total_available": total_available,
        "total_reserved": total_reserved,
        "total_spendable": total_available,
        "usage_percent": usage_percent,
        "breakdown": breakdown,
        "bucket_policy": {
            "included": "Subscription plan credits. Valid until the current billing period ends or renews.",
            "purchased": "Top-up, migrated, or carried-over wallet credits. No expiry unless an expiry is explicitly set.",
            "promo": "Promotional or admin-granted credits. Promo expiry is shown when present.",
        },
        "source": "v_pricing_account_overview",
    }

    header = dict(overview_header)
    header.update(
        {
            "plan_label": plan_name,
            "plan_name": plan_name,
            "usage_label": display["header_label"],
            "billing_value_label": display["header_label"],
            "header_label": display["header_label"],
            "available_credits": total_available,
            "reserved_credits": total_reserved,
            "total_credits": plan_total,
            "billing_model": billing_model,
        }
    )

    overview["billing_model"] = billing_model
    overview["billing_mode"] = canonical_plan["billing_mode"]
    overview["settlement_mode"] = canonical_plan["settlement_mode"]
    overview["plan"] = canonical_plan
    overview["credits"] = canonical_credits
    overview["header"] = header
    overview["display"] = display
    overview["integrity"] = {
        "source": "canonical_credit_display",
        "entitlement_remaining_legacy": ent.get("included_credits_remaining"),
        "live_total_available": total_available,
    }
    return overview
