from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.services.engine.module_gate import evaluate_gate


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


async def _table_exists(conn: asyncpg.Connection, table_name: str) -> bool:
    row = await conn.fetchrow("select to_regclass($1) as reg", table_name)
    return bool(row and row.get("reg"))


async def _resolve_tier_code(conn: asyncpg.Connection, user_id: UUID) -> str:
    ent = await conn.fetchrow(
        "select tier_code from pricing_user_entitlements where user_id=$1",
        user_id,
    )
    if ent and ent.get("tier_code"):
        return str(ent["tier_code"])

    user_row = await conn.fetchrow(
        "select tier from core.users where id=$1",
        user_id,
    )
    if user_row and user_row.get("tier"):
        return str(user_row["tier"])

    return "free"


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


def _normalize_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in {"bill", "paid", "charge", "usage_bill"}:
        return "bill"
    if v in {"included", "free", "shadow", "allow", "usage_only"}:
        return "included"
    if v in {"blocked", "block", "deny", "disabled", "off"}:
        return "blocked"
    return ""


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
    tier_code = await _resolve_tier_code(conn, user_id)

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
        )

    module_code = f"module.{category}"
    gate = await evaluate_gate(
        conn,
        module_code=module_code,
        channel=channel,
        country_code=country_code,
        tier_code=tier_code,
    )
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
            meta={"gate_billing_mode": str(getattr(gate, "billing_mode", "") or "")},
        )

    override_row = await _fetch_account_override(
        conn,
        user_id=user_id,
        service_name=service_name,
        service_action=service_action,
        sku_code=sku_code,
    )
    if override_row:
        mode = _normalize_mode(
            override_row.get("mode")
            or override_row.get("billing_mode")
            or override_row.get("billing_mode_override")
        )
        if not mode:
            mode = _normalize_mode(getattr(gate, "billing_mode", "")) or "included"
        return ResolvedEntitlement(
            allowed=(mode != "blocked"),
            billing_mode=mode,
            pricing_mode="bill" if mode == "bill" else ("free" if mode == "included" else "disabled"),
            source="account_override",
            tier_code=tier_code,
            module_code=module_code,
            category=category,
            reason=None if mode != "blocked" else "ENTITLEMENT_BLOCKED_ACCOUNT_OVERRIDE",
            included_units=(str(override_row.get("included_units_override")) if override_row.get("included_units_override") is not None else None),
            allow_overage_bill=bool(override_row.get("allow_overage_bill_override") or False),
            rule_id=str(override_row.get("id")) if override_row.get("id") is not None else None,
            meta={"gate_billing_mode": str(getattr(gate, "billing_mode", "") or "")},
        )

    tier_row = await _fetch_tier_default(
        conn,
        tier_code=tier_code,
        service_name=service_name,
        service_action=service_action,
        sku_code=sku_code,
    )
    if tier_row:
        mode = _normalize_mode(
            tier_row.get("mode")
            or tier_row.get("billing_mode")
        )
        if not mode:
            mode = _normalize_mode(getattr(gate, "billing_mode", "")) or "included"
        return ResolvedEntitlement(
            allowed=(mode != "blocked"),
            billing_mode=mode,
            pricing_mode="bill" if mode == "bill" else ("free" if mode == "included" else "disabled"),
            source="tier_default",
            tier_code=tier_code,
            module_code=module_code,
            category=category,
            reason=None if mode != "blocked" else "ENTITLEMENT_BLOCKED_TIER_DEFAULT",
            included_units=(str(tier_row.get("included_units")) if tier_row.get("included_units") is not None else None),
            allow_overage_bill=bool(tier_row.get("allow_overage_bill") or False),
            rule_id=str(tier_row.get("id")) if tier_row.get("id") is not None else None,
            meta={"gate_billing_mode": str(getattr(gate, "billing_mode", "") or "")},
        )

    fallback_mode = _normalize_mode(getattr(gate, "billing_mode", ""))
    if not fallback_mode:
        raw_gate_mode = str(getattr(gate, "billing_mode", "") or "").strip().lower()
        if raw_gate_mode == "bill":
            fallback_mode = "bill"
        elif raw_gate_mode in {"disabled", "blocked"}:
            fallback_mode = "blocked"
        else:
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
        meta={"gate_billing_mode": str(getattr(gate, "billing_mode", "") or "")},
    )