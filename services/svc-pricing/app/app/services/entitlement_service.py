
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.repo.billing_entitlements_repo import BillingEntitlementsRepo, BillingEntitlementRow
from app.repo.entitlements_repo import EntitlementsRepo, FeatureFlagRow
from app.services.engine.module_gate import evaluate_gate


_ENTITLEMENTS_REPO = EntitlementsRepo()
_BILLING_ENTITLEMENTS_REPO = BillingEntitlementsRepo()


@dataclass(frozen=True)
class ResolvedEntitlement:
    allowed: bool
    billing_mode: str
    pricing_mode: str
    source: str
    tier_code: str
    module_code: str
    category: str
    reason: Optional[str] = None
    included_units: Optional[str] = None
    allow_overage_bill: bool = False
    rule_id: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


def _normalize_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in {"bill", "paid", "charge", "usage_bill"}:
        return "bill"
    if v in {"included", "free", "shadow", "allow", "usage_only"}:
        return "included"
    if v in {"blocked", "block", "deny", "disabled", "off"}:
        return "blocked"
    return ""


def _normalize_settlement_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in {"postpaid", "invoice", "bill", "billed", "money"}:
        return "postpaid"
    if v in {"prepaid", "credit", "credits", "wallet", "payg"}:
        return "prepaid"
    if v in {"hybrid", "mixed"}:
        return "hybrid"
    return ""


def _preferred_feature_mode(
    feature_flag: Optional[FeatureFlagRow],
    gate_billing_mode: str,
) -> str:
    feature_mode = _normalize_mode(feature_flag.billing_mode if feature_flag else "")
    if feature_mode:
        return feature_mode

    gate_mode = _normalize_mode(gate_billing_mode)
    if gate_mode:
        return gate_mode

    raw_gate_mode = str(gate_billing_mode or "").strip().lower()
    if raw_gate_mode == "bill":
        return "bill"
    if raw_gate_mode in {"disabled", "blocked"}:
        return "blocked"
    return ""


def _feature_code_for_request(*, service_name: str, service_action: str, sku_code: str) -> str:
    service = str(service_name or "").strip().lower()
    action = str(service_action or "").strip().lower()
    sku = str(sku_code or "").strip().lower()
    blob = " ".join(part for part in (service, action, sku) if part)

    if "svc-face" in service or service == "face" or service.startswith("svc-face"):
        return "FACE_STUDIO"
    if "svc-audio" in service or service == "audio" or service.startswith("svc-audio"):
        return "AUDIO_STUDIO"
    if "svc-fusion-extension" in service or service.startswith("svc-fusion-extension"):
        if any(token in blob for token in ("cinematic", "direction", "longform_cinematic")):
            return "CINEMATIC_VIDEO_DIRECTION"
        return "TALKING_VIDEO"
    if "svc-fusion" in service or service == "fusion" or service.startswith("svc-fusion"):
        return "TALKING_VIDEO"
    return ""


async def _table_exists(conn: asyncpg.Connection, table_name: str) -> bool:
    row = await conn.fetchrow("select to_regclass($1) as reg", table_name)
    return bool(row and row.get("reg"))


async def _variant_category(conn: asyncpg.Connection, sku_code: str) -> Optional[str]:
    row = await conn.fetchrow(
        "select category from pricing_variants where code=$1 and is_active=true",
        sku_code,
    )
    if not row or not row.get("category"):
        return None
    return str(row["category"])


async def _fetch_account_override(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    service_name: str,
    service_action: str,
    sku_code: str,
) -> Optional[asyncpg.Record]:
    if not await _table_exists(conn, "pricing_account_entitlement_overrides"):
        return None

    return await conn.fetchrow(
        """
        select *
        from pricing_account_entitlement_overrides
        where user_id = $1
          and coalesce(is_enabled, true) = true
          and (service_name = $2 or service_name = '*' or service_name is null)
          and (service_action = $3 or service_action = '*' or service_action is null)
          and (sku_code = $4 or sku_code = '*' or sku_code is null)
        order by
          case when service_name = $2 then 0 when service_name = '*' then 1 else 2 end,
          case when service_action = $3 then 0 when service_action = '*' then 1 else 2 end,
          case when sku_code = $4 then 0 when sku_code = '*' then 1 else 2 end,
          coalesce(priority, 100),
          created_at desc nulls last
        limit 1
        """,
        user_id,
        service_name,
        service_action,
        sku_code,
    )


async def _fetch_tier_default(
    conn: asyncpg.Connection,
    *,
    tier_code: str,
    service_name: str,
    service_action: str,
    sku_code: str,
) -> Optional[asyncpg.Record]:
    if not await _table_exists(conn, "pricing_tier_entitlements"):
        return None

    return await conn.fetchrow(
        """
        select *
        from pricing_tier_entitlements
        where tier_code = $1
          and coalesce(is_enabled, true) = true
          and (service_name = $2 or service_name = '*' or service_name is null)
          and (service_action = $3 or service_action = '*' or service_action is null)
          and (sku_code = $4 or sku_code = '*' or sku_code is null)
        order by
          case when service_name = $2 then 0 when service_name = '*' then 1 else 2 end,
          case when service_action = $3 then 0 when service_action = '*' then 1 else 2 end,
          case when sku_code = $4 then 0 when sku_code = '*' then 1 else 2 end,
          coalesce(priority, 100),
          created_at desc nulls last
        limit 1
        """,
        tier_code,
        service_name,
        service_action,
        sku_code,
    )


def _from_override_row(
    row: asyncpg.Record,
    *,
    tier_code: str,
    module_code: str,
    category: str,
    gate_billing_mode: str,
    feature_code: str = "",
    feature_flag: Optional[FeatureFlagRow] = None,
) -> ResolvedEntitlement:
    mode = _normalize_mode(
        row.get("mode")
        or row.get("billing_mode")
        or row.get("billing_mode_override")
    )
    if not mode:
        mode = _preferred_feature_mode(feature_flag, gate_billing_mode) or "included"
    return ResolvedEntitlement(
        allowed=(mode != "blocked"),
        billing_mode=mode,
        pricing_mode="bill" if mode == "bill" else ("free" if mode == "included" else "disabled"),
        source="account_override",
        tier_code=tier_code,
        module_code=module_code,
        category=category,
        reason=None if mode != "blocked" else "ENTITLEMENT_BLOCKED_ACCOUNT_OVERRIDE",
        included_units=(str(row.get("included_units_override")) if row.get("included_units_override") is not None else None),
        allow_overage_bill=bool(row.get("allow_overage_bill_override") or False),
        rule_id=str(row.get("id")) if row.get("id") is not None else None,
        meta={
            "feature_code": feature_code or None,
            "feature_flag_billing_mode": (feature_flag.billing_mode if feature_flag else None),
            "gate_billing_mode": gate_billing_mode,
            "source_table": "pricing_account_entitlement_overrides",
        },
    )


def _from_billing_entitlement(
    row: BillingEntitlementRow,
    *,
    tier_code: str,
    module_code: str,
    category: str,
    gate_billing_mode: str,
    feature_code: str = "",
    feature_flag: Optional[FeatureFlagRow] = None,
) -> ResolvedEntitlement:
    ent_billing_mode = str(row.billing_mode or "").strip().lower()
    settlement_mode = _normalize_settlement_mode(row.settlement_mode) or "prepaid"
    overage_allowed = bool(row.overage_allowed or False)
    included_remaining = row.included_credits_remaining
    wallet_topup_allowed = bool(row.wallet_topup_allowed if row.wallet_topup_allowed is not None else True)
    hard_stop = bool(row.hard_stop_on_insufficient_balance if row.hard_stop_on_insufficient_balance is not None else True)

    feature_mode = _preferred_feature_mode(feature_flag, gate_billing_mode)

    # Production rule:
    # - feature flags / module gate decide whether a feature is billable vs included vs blocked
    # - billing_entitlements decide settlement mechanics and account policy
    # This prevents a free-tier entitlement row from incorrectly forcing billable features
    # like FACE_STUDIO / AUDIO_STUDIO into pricing_mode="free".
    if feature_mode == "blocked":
        mode = "blocked"
        pricing_mode = "disabled"
    elif feature_mode in {"bill", "included"}:
        mode = feature_mode
        pricing_mode = "bill" if mode == "bill" else "free"
    elif ent_billing_mode == "postpaid":
        mode = "bill"
        pricing_mode = "bill"
    elif ent_billing_mode in {"prepaid", "subscription", "hybrid"}:
        mode = "bill"
        pricing_mode = "bill"
    elif ent_billing_mode == "free":
        mode = "included"
        pricing_mode = "free"
    else:
        mode = "included"
        pricing_mode = "free"

    if mode == "blocked":
        return ResolvedEntitlement(
            allowed=False,
            billing_mode="blocked",
            pricing_mode="disabled",
            source="billing_entitlements",
            tier_code=tier_code,
            module_code=module_code,
            category=category,
            reason="ENTITLEMENT_BLOCKED_BILLING_ENTITLEMENT",
            included_units=(str(included_remaining) if included_remaining is not None else None),
            allow_overage_bill=overage_allowed,
            rule_id=None,
            meta={
                "feature_code": feature_code or None,
                "feature_flag_billing_mode": (feature_flag.billing_mode if feature_flag else None),
                "feature_mode_effective": feature_mode or None,
                "gate_billing_mode": gate_billing_mode,
                "source_table": "billing_entitlements",
                "settlement_mode_hint": settlement_mode,
                "plan_code": row.plan_code,
                "wallet_topup_allowed": wallet_topup_allowed,
                "hard_stop_on_insufficient_balance": hard_stop,
            },
        )

    return ResolvedEntitlement(
        allowed=True,
        billing_mode=mode,
        pricing_mode=pricing_mode,
        source="billing_entitlements",
        tier_code=str(row.tier_code or tier_code),
        module_code=module_code,
        category=category,
        reason=None,
        included_units=(str(included_remaining) if included_remaining is not None else None),
        allow_overage_bill=overage_allowed,
        rule_id=None,
        meta={
            "feature_code": feature_code or None,
            "feature_flag_billing_mode": (feature_flag.billing_mode if feature_flag else None),
            "feature_mode_effective": feature_mode or None,
            "gate_billing_mode": gate_billing_mode,
            "source_table": "billing_entitlements",
            "entitlement_billing_mode": ent_billing_mode,
            "settlement_mode_hint": settlement_mode,
            "plan_code": row.plan_code,
            "wallet_topup_allowed": wallet_topup_allowed,
            "hard_stop_on_insufficient_balance": hard_stop,
            "included_credits_total": str(row.included_credits_total) if row.included_credits_total is not None else None,
            "included_credits_remaining": str(included_remaining) if included_remaining is not None else None,
        },
    )


def _from_tier_default(
    row: asyncpg.Record,
    *,
    tier_code: str,
    module_code: str,
    category: str,
    gate_billing_mode: str,
    feature_code: str = "",
    feature_flag: Optional[FeatureFlagRow] = None,
) -> ResolvedEntitlement:
    mode = _normalize_mode(
        row.get("mode")
        or row.get("billing_mode")
    )
    if not mode:
        mode = _preferred_feature_mode(feature_flag, gate_billing_mode) or "included"
    return ResolvedEntitlement(
        allowed=(mode != "blocked"),
        billing_mode=mode,
        pricing_mode="bill" if mode == "bill" else ("free" if mode == "included" else "disabled"),
        source="tier_default",
        tier_code=tier_code,
        module_code=module_code,
        category=category,
        reason=None if mode != "blocked" else "ENTITLEMENT_BLOCKED_TIER_DEFAULT",
        included_units=(str(row.get("included_units")) if row.get("included_units") is not None else None),
        allow_overage_bill=bool(row.get("allow_overage_bill") or False),
        rule_id=str(row.get("id")) if row.get("id") is not None else None,
        meta={
            "feature_code": feature_code or None,
            "feature_flag_billing_mode": (feature_flag.billing_mode if feature_flag else None),
            "gate_billing_mode": gate_billing_mode,
            "source_table": "pricing_tier_entitlements",
        },
    )


async def resolve_entitlement(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    service_name: str,
    service_action: str,
    sku_code: str,
    channel: str,
    country_code: str,
) -> ResolvedEntitlement:
    # Canonical production rule:
    # billing_entitlements is the source of truth for the user's current paid/free
    # tier after Stripe, Apple, Google Play, or admin subscription changes.
    # pricing_user_entitlements is retained as a compatibility cache only.
    # Feature flags and module gates must evaluate with this canonical tier;
    # otherwise paid users can show Pro/Business in the header while studios are
    # still blocked by a stale legacy tier.
    billing_ent_row = await _BILLING_ENTITLEMENTS_REPO.get_active_by_user_id(conn, user_id=user_id)

    legacy_tier_code = await _ENTITLEMENTS_REPO.resolve_tier_code(
        conn,
        user_id=user_id,
        fallback_to_core_user_tier=True,
        ensure_default_free=False,
    )
    canonical_billing_tier = str(getattr(billing_ent_row, "tier_code", "") or "").strip().lower() if billing_ent_row else ""
    tier_code = canonical_billing_tier or str(legacy_tier_code or "free").strip().lower() or "free"

    feature_code = _feature_code_for_request(
        service_name=service_name,
        service_action=service_action,
        sku_code=sku_code,
    )
    feature_flag = None
    if feature_code:
        existing_feature_flag = await _ENTITLEMENTS_REPO.get_feature_flag(conn, code=feature_code)
        if existing_feature_flag is not None:
            feature_flag = await _ENTITLEMENTS_REPO.resolve_feature_flag(
                conn,
                code=feature_code,
                tier_code=tier_code,
                country_code=country_code,
                channel=channel,
            )
            if feature_flag is None:
                return ResolvedEntitlement(
                    allowed=False,
                    billing_mode="blocked",
                    pricing_mode="disabled",
                    source="feature_flag",
                    tier_code=tier_code,
                    module_code="",
                    category="",
                    reason="ENTITLEMENT_BLOCKED_FEATURE_FLAG",
                    meta={
                        "feature_code": feature_code,
                        "tier_source": "billing_entitlements" if canonical_billing_tier else "legacy_entitlements",
                        "legacy_tier_code": str(legacy_tier_code or ""),
                        "billing_plan_code": (billing_ent_row.plan_code if billing_ent_row else None),
                    },
                )

    category = await _variant_category(conn, sku_code)
    if not category:
        return ResolvedEntitlement(
            allowed=False,
            billing_mode="blocked",
            pricing_mode="disabled",
            source="variant_lookup",
            tier_code=tier_code,
            module_code="",
            category="",
            reason="PRICING_UNKNOWN_OR_INACTIVE_VARIANT",
            meta={"feature_code": feature_code or None},
        )

    module_code = f"module.{category}"
    gate = await evaluate_gate(
        conn,
        module_code=module_code,
        channel=channel,
        country_code=country_code,
        tier_code=tier_code,
    )
    gate_billing_mode = str(getattr(gate, "billing_mode", "") or "")
    if not gate.allowed:
        return ResolvedEntitlement(
            allowed=False,
            billing_mode="blocked",
            pricing_mode="disabled",
            source="module_gate",
            tier_code=tier_code,
            module_code=module_code,
            category=category,
            reason=str(gate.reason or "module_disabled"),
            meta={
                "feature_code": feature_code or None,
                "feature_flag_billing_mode": (feature_flag.billing_mode if feature_flag else None),
                "gate_billing_mode": gate_billing_mode,
            },
        )

    override_row = await _fetch_account_override(
        conn,
        user_id=user_id,
        service_name=service_name,
        service_action=service_action,
        sku_code=sku_code,
    )
    if override_row:
        return _from_override_row(
            override_row,
            tier_code=tier_code,
            module_code=module_code,
            category=category,
            gate_billing_mode=gate_billing_mode,
            feature_code=feature_code,
            feature_flag=feature_flag,
        )

    if billing_ent_row:
        return _from_billing_entitlement(
            billing_ent_row,
            tier_code=tier_code,
            module_code=module_code,
            category=category,
            gate_billing_mode=gate_billing_mode,
            feature_code=feature_code,
            feature_flag=feature_flag,
        )

    tier_row = await _fetch_tier_default(
        conn,
        tier_code=tier_code,
        service_name=service_name,
        service_action=service_action,
        sku_code=sku_code,
    )
    if tier_row:
        return _from_tier_default(
            tier_row,
            tier_code=tier_code,
            module_code=module_code,
            category=category,
            gate_billing_mode=gate_billing_mode,
            feature_code=feature_code,
            feature_flag=feature_flag,
        )

    fallback_mode = _preferred_feature_mode(feature_flag, gate_billing_mode)
    if not fallback_mode:
        fallback_mode = "included"

    return ResolvedEntitlement(
        allowed=(fallback_mode != "blocked"),
        billing_mode=fallback_mode,
        pricing_mode="bill" if fallback_mode == "bill" else ("free" if fallback_mode == "included" else "disabled"),
        source="module_gate_fallback",
        tier_code=tier_code,
        module_code=module_code,
        category=category,
        reason=None if fallback_mode != "blocked" else str(gate.reason or "ENTITLEMENT_BLOCKED_MODULE_GATE"),
        meta={
            "feature_code": feature_code or None,
            "feature_flag_billing_mode": (feature_flag.billing_mode if feature_flag else None),
            "gate_billing_mode": gate_billing_mode,
            "tier_source": "billing_entitlements" if canonical_billing_tier else "legacy_entitlements",
            "legacy_tier_code": str(legacy_tier_code or ""),
            "billing_plan_code": (billing_ent_row.plan_code if billing_ent_row else None),
        },
    )
