# services/svc-pricing/app/app/services/engine/pricing_engine.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceLine:
    sku_code: str
    name: str
    category: str
    provider_hint: Optional[str]
    unit: str
    qty: Decimal
    unit_credits: int
    line_credits: int
    unit_money: Optional[Decimal]
    line_money: Decimal


@dataclass(frozen=True)
class QuoteResult:
    variant_code: str
    category: str
    currency: str
    pricebook_id: UUID
    pricebook_name: str
    billing_mode: str
    total_credits: int
    total_money: Decimal
    rounding_mode: str
    lines: List[PriceLine]
    alt_currency: Optional[str] = None
    alt_total_money: Optional[Decimal] = None


def _d(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _ceil_int(x: Decimal) -> int:
    if x <= 0:
        return 0
    return int(x.to_integral_value(rounding=ROUND_CEILING))


def _round_money(amount: Decimal, mode: str) -> Decimal:
    q = Decimal("1").scaleb(-settings.MONEY_DECIMALS)  # 0.01
    mode = (mode or "ceil").lower()
    if mode == "floor":
        return amount.quantize(q, rounding=ROUND_FLOOR)
    if mode == "round":
        return amount.quantize(q, rounding=ROUND_HALF_UP)
    # default ceil
    return amount.quantize(q, rounding=ROUND_CEILING)


async def _resolve_tier_code(conn: asyncpg.Connection, user_id: UUID) -> str:
    r = await conn.fetchrow(
        "select tier_code from pricing_user_entitlements where user_id = $1",
        user_id,
    )
    if not r:
        return "free"
    return str(r["tier_code"] or "free")


async def _get_credit_value(conn: asyncpg.Connection, currency: str) -> Tuple[Decimal, str]:
    r = await conn.fetchrow(
        """
        select money_per_credit, rounding_mode
        from pricing_credit_value
        where currency = $1
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by effective_from desc
        limit 1
        """,
        currency,
    )
    if not r:
        # safe fallback
        return Decimal("0.01"), "ceil"
    return _d(r["money_per_credit"]), str(r["rounding_mode"] or "ceil")


async def _select_pricebook(
    conn: asyncpg.Connection,
    *,
    currency: str,
    channel: str,
    country_code: str,
    tier_code: str,
) -> dict:
    """
    Deterministic selection:
      - must be active, within effective window
      - match currency + channel
      - prefer exact country match, else null country_code (global)
      - prefer exact tier match, else null tier_code (all tiers)
      - latest effective_from
    """
    rows = await conn.fetch(
        """
        select *
        from pricing_pricebooks
        where is_active = true
          and currency = $1
          and channel = $2
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
          and (country_code is null or country_code = $3)
          and (tier_code is null or tier_code = $4)
        order by
          (country_code = $3) desc,
          (tier_code = $4) desc,
          effective_from desc,
          created_at desc
        limit 1
        """,
        currency, channel, (country_code or None), (tier_code or None),
    )
    if not rows:
        # fallback: allow global tier/country
        rows = await conn.fetch(
            """
            select *
            from pricing_pricebooks
            where is_active = true
              and currency = $1
              and channel = $2
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc, created_at desc
            limit 1
            """,
            currency, channel,
        )
    if not rows:
        raise ValueError("PRICING_NO_ACTIVE_PRICEBOOK")
    return dict(rows[0])


async def _get_variant(conn: asyncpg.Connection, variant_code: str) -> dict:
    r = await conn.fetchrow(
        "select code, name, category, is_active, metadata_json from pricing_variants where code = $1",
        variant_code,
    )
    if not r:
        raise ValueError("PRICING_UNKNOWN_VARIANT")
    d = dict(r)
    if not bool(d.get("is_active", True)):
        raise ValueError("PRICING_VARIANT_INACTIVE")
    return d


async def _get_variant_lines(conn: asyncpg.Connection, variant_code: str) -> list[dict]:
    rows = await conn.fetch(
        """
        select variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json
        from pricing_variant_lines
        where variant_code = $1
        """,
        variant_code,
    )
    return [dict(x) for x in rows]


async def _get_sku(conn: asyncpg.Connection, sku_code: str) -> dict:
    r = await conn.fetchrow(
        """
        select code, name, unit, category, provider_hint, default_unit_credits, status, metadata_json
        from pricing_skus where code = $1
        """,
        sku_code,
    )
    if not r:
        raise ValueError("PRICING_UNKNOWN_SKU")
    d = dict(r)
    if str(d.get("status") or "active") != "active":
        raise ValueError("PRICING_SKU_INACTIVE")
    return d


async def _get_sku_override(conn: asyncpg.Connection, pricebook_id: UUID, sku_code: str) -> dict:
    r = await conn.fetchrow(
        """
        select unit_credits_override, unit_money_override, min_qty, max_qty, metadata_json
        from pricing_sku_prices
        where pricebook_id = $1 and sku_code = $2
        """,
        pricebook_id, sku_code,
    )
    return dict(r) if r else {}


def _qty_from_params(line: dict, params: Dict[str, Any]) -> Decimal:
    mode = str(line.get("qty_mode") or "fixed")
    if mode == "fixed":
        return _d(line.get("qty_value") or 0)
    if mode == "param":
        key = str(line.get("qty_param") or "").strip()
        if not key:
            return Decimal("0")
        return _d(params.get(key, 0))
    # metered lines are 0 at quote time unless caller supplies actuals later
    return Decimal("0")


def _primary_currency(country_code: str, requested_currency: Optional[str]) -> str:
    if requested_currency:
        return requested_currency.upper()
    return "INR" if (country_code or "").upper() == "IN" else "USD"


async def _resolve_tier_code(conn: asyncpg.Connection, user_id: UUID) -> str:
    # 1) pricing_user_entitlements wins (supports future tiers like "developer")
    r = await conn.fetchrow(
        "select tier_code from pricing_user_entitlements where user_id = $1",
        user_id,
    )
    if r and r.get("tier_code"):
        return str(r["tier_code"])

    # 2) fallback to core.users.tier (free|pro|enterprise)
    u = await conn.fetchrow(
        "select tier from core.users where id = $1",
        user_id,
    )
    if u and u.get("tier"):
        return str(u["tier"])

    return "free"


async def quote_variant(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    variant_code: str,
    params: Dict[str, Any],
    channel: str,                     # web|mobile|api
    country_code: str,
    currency: Optional[str],
    billing_mode: str,                # from module_gate
) -> QuoteResult:
    """
    Computes quote using:
      - variant BOM expansion (pricing_variant_lines)
      - SKU base credits (pricing_skus.default_unit_credits)
      - pricebook selection + overrides + multiplier
      - credit->money conversion via pricing_credit_value
    """
    var = await _get_variant(conn, variant_code)
    tier_code = await _resolve_tier_code(conn, user_id)

    cur = _primary_currency(country_code, currency)
    pb = await _select_pricebook(conn, currency=cur, channel=channel, country_code=country_code, tier_code=tier_code)
    pb_id = UUID(str(pb["id"]))
    pb_mult = _d(pb.get("multiplier") or 1)

    money_per_credit, rounding_mode = await _get_credit_value(conn, cur)

    lines_def = await _get_variant_lines(conn, variant_code)
    if not lines_def:
        raise ValueError("PRICING_VARIANT_HAS_NO_LINES")

    lines: List[PriceLine] = []
    total_credits = 0

    for ld in lines_def:
        sku_code = str(ld["sku_code"])
        sku = await _get_sku(conn, sku_code)
        ov = await _get_sku_override(conn, pb_id, sku_code)

        qty = _qty_from_params(ld, params)
        if qty <= 0:
            continue

        unit_credits = int(ov.get("unit_credits_override") or sku["default_unit_credits"])
        raw_credits = qty * _d(unit_credits) * pb_mult
        line_credits = _ceil_int(raw_credits)

        # If billing_mode is shadow/free, we still compute "would-have-cost" in metadata,
        # but we return total_credits=0 and total_money=0 to enforce no-charge behavior.
        # For UX: frontend can still display the "shadow_estimate" from line metadata if desired.
        unit_money_override = ov.get("unit_money_override")
        if unit_money_override is not None:
            unit_money = _d(unit_money_override)
            line_money = _round_money(unit_money * qty, rounding_mode)
        else:
            unit_money = None
            line_money = _round_money(_d(line_credits) * money_per_credit, rounding_mode)

        lines.append(
            PriceLine(
                sku_code=sku_code,
                name=str(sku["name"]),
                category=str(sku["category"]),
                provider_hint=sku.get("provider_hint"),
                unit=str(sku["unit"]),
                qty=qty,
                unit_credits=unit_credits,
                line_credits=line_credits,
                unit_money=unit_money,
                line_money=line_money,
            )
        )
        total_credits += line_credits

    total_money = _round_money(_d(total_credits) * money_per_credit, rounding_mode)
    # If any overrides set unit_money, our per-line money already accounts; so sum line_money.
    if any(l.unit_money is not None for l in lines):
        total_money = _round_money(sum((l.line_money for l in lines), Decimal("0")), rounding_mode)

    # Apply no-charge modes
    if billing_mode in {"shadow", "free"}:
        total_credits = 0
        total_money = _round_money(Decimal("0"), rounding_mode)

    # Alt-currency preview (optional)
    alt_currency = "USD" if cur == "INR" else "INR"
    try:
        alt_mpc, alt_round = await _get_credit_value(conn, alt_currency)
        alt_total = _round_money(_d(total_credits) * alt_mpc, alt_round)
    except Exception:
        alt_total = None

    return QuoteResult(
        variant_code=variant_code,
        category=str(var["category"]),
        currency=cur,
        pricebook_id=pb_id,
        pricebook_name=str(pb["name"]),
        billing_mode=billing_mode,
        total_credits=int(total_credits),
        total_money=total_money,
        rounding_mode=rounding_mode,
        lines=lines,
        alt_currency=alt_currency if alt_total is not None else None,
        alt_total_money=alt_total,
    )