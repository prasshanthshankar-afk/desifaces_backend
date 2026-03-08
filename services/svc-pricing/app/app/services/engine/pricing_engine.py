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


# -------------------------
# models
# -------------------------

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

    # “Would-have-cost” (useful for free/shadow UX)
    shadow_total_credits: Optional[int] = None
    shadow_total_money: Optional[Decimal] = None

    # Optional alternate currency preview
    alt_currency: Optional[str] = None
    alt_total_money: Optional[Decimal] = None


# -------------------------
# helpers
# -------------------------

def _norm_currency(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    v = x.strip().upper()
    return v or None


def _norm_country(x: str) -> str:
    return (x or "").strip().upper()


def _norm_channel(x: str) -> str:
    v = (x or "web").strip().lower()
    return v if v in {"web", "mobile", "api"} else "web"


def _d(x: Any) -> Decimal:
    # for internal known-numeric DB values
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _d_param(x: Any, key: str) -> Decimal:
    # for user-provided params: fail closed on invalid values
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        raise ValueError(f"PRICING_BAD_PARAM:{key}")


def _ceil_int(x: Decimal) -> int:
    if x <= 0:
        return 0
    return int(x.to_integral_value(rounding=ROUND_CEILING))


def _round_money(amount: Decimal, mode: str) -> Decimal:
    q = Decimal("1").scaleb(-settings.MONEY_DECIMALS)  # e.g. 0.01 for 2 dp
    mode = (mode or "ceil").lower()
    if mode == "floor":
        return amount.quantize(q, rounding=ROUND_FLOOR)
    if mode == "round":
        return amount.quantize(q, rounding=ROUND_HALF_UP)
    return amount.quantize(q, rounding=ROUND_CEILING)


def _primary_currency(country_code: str, requested_currency: Optional[str]) -> str:
    rc = _norm_currency(requested_currency)
    if rc:
        return rc
    return "INR" if _norm_country(country_code) == "IN" else "USD"


def _norm_billing_mode(x: str) -> str:
    v = (x or "bill").strip().lower()
    if v in {"bill", "shadow", "free", "disabled"}:
        return v
    return "bill"


async def _resolve_tier_code(conn: asyncpg.Connection, user_id: UUID) -> str:
    # 1) pricing_user_entitlements wins (supports future tiers like developer/api_enterprise)
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
        # FAIL CLOSED: missing credit value means we'd produce wrong money numbers
        raise ValueError(f"PRICING_MISSING_CREDIT_VALUE:{currency}")
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
      - active, within effective window
      - match currency + channel
      - allow country_code null/global or exact match
      - allow tier_code null/all tiers or exact match
      - prefer exact match, then newest effective_from
    """
    cc = _norm_country(country_code)
    tc = (tier_code or "").strip().lower() or "free"
    ch = _norm_channel(channel)

    row = await conn.fetchrow(
        """
        select *
        from pricing_pricebooks
        where is_active = true
          and currency = $1
          and channel = $2
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
          and (country_code is null or country_code = $3 or $3 = '')
          and (tier_code is null or tier_code = $4)
        order by
          case when $3 <> '' and country_code = $3 then 1 else 0 end desc,
          case when tier_code = $4 then 1 else 0 end desc,
          effective_from desc,
          created_at desc
        limit 1
        """,
        currency,
        ch,
        cc,
        tc,
    )
    if not row:
        raise ValueError("PRICING_NO_ACTIVE_PRICEBOOK")
    return dict(row)


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
        order by sku_code asc
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
        pricebook_id,
        sku_code,
    )
    return dict(r) if r else {}


def _qty_from_params(line: dict, params: Dict[str, Any]) -> Decimal:
    mode = str(line.get("qty_mode") or "fixed").lower()

    if mode == "fixed":
        return _d(line.get("qty_value") or 0)

    if mode == "param":
        key = str(line.get("qty_param") or "").strip()
        if not key:
            return Decimal("0")
        return _d_param(params.get(key, 0), key)

    # metered lines: 0 at quote time (actuals used at finalize later)
    return Decimal("0")


# -------------------------
# main entry
# -------------------------

async def quote_variant(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    variant_code: str,
    params: Dict[str, Any],
    channel: str,          # web|mobile|api
    country_code: str,
    currency: Optional[str],
    billing_mode: str,     # from module_gate
) -> QuoteResult:
    var = await _get_variant(conn, variant_code)
    tier_code = await _resolve_tier_code(conn, user_id)

    cc = _norm_country(country_code)
    ch = _norm_channel(channel)
    cur = _primary_currency(cc, currency)

    pb = await _select_pricebook(conn, currency=cur, channel=ch, country_code=cc, tier_code=tier_code)
    pb_id = UUID(str(pb["id"]))
    pb_mult = _d(pb.get("multiplier") or 1)

    money_per_credit, rounding_mode = await _get_credit_value(conn, cur)

    lines_def = await _get_variant_lines(conn, variant_code)
    if not lines_def:
        raise ValueError("PRICING_VARIANT_HAS_NO_LINES")

    lines: List[PriceLine] = []
    bill_total_credits = 0
    bill_total_money = Decimal("0")

    for ld in lines_def:
        sku_code = str(ld["sku_code"])
        sku = await _get_sku(conn, sku_code)
        ov = await _get_sku_override(conn, pb_id, sku_code)

        qty = _qty_from_params(ld, params)
        if qty <= 0:
            continue

        # Sanity: unit credits cannot be negative
        unit_credits = int(ov.get("unit_credits_override") or sku["default_unit_credits"] or 0)
        unit_credits = max(0, unit_credits)

        # Credits (multiplied)
        raw_credits = qty * _d(unit_credits) * pb_mult
        line_credits = _ceil_int(raw_credits)

        # Money policy:
        # - If unit_money_override exists AND qty bounds allow, use it
        # - Else derive from credits * money_per_credit
        unit_money_override = ov.get("unit_money_override")
        min_qty = ov.get("min_qty")
        max_qty = ov.get("max_qty")

        if unit_money_override is not None:
            if (min_qty is not None and qty < _d(min_qty)) or (max_qty is not None and qty > _d(max_qty)):
                unit_money_override = None

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

        bill_total_credits += line_credits
        bill_total_money += line_money

    if not lines:
        raise ValueError("PRICING_VARIANT_ZERO_QTY_LINES")

    # Total money is sum(line_money) so breakdown always matches total.
    bill_total_money = _round_money(bill_total_money, rounding_mode)

    bm = _norm_billing_mode(billing_mode)
    shadow_total_credits = bill_total_credits
    shadow_total_money = bill_total_money

    if bm in {"shadow", "free", "disabled"}:
        total_credits = 0
        total_money = _round_money(Decimal("0"), rounding_mode)
    else:
        total_credits = bill_total_credits
        total_money = bill_total_money

    # Alt currency preview of the *charged* amount (based on total_credits)
    alt_currency = "USD" if cur == "INR" else "INR"
    alt_total = None
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
        billing_mode=bm,
        total_credits=int(total_credits),
        total_money=total_money,
        rounding_mode=rounding_mode,
        lines=lines,
        shadow_total_credits=int(shadow_total_credits),
        shadow_total_money=shadow_total_money,
        alt_currency=alt_currency if alt_total is not None else None,
        alt_total_money=alt_total,
    )