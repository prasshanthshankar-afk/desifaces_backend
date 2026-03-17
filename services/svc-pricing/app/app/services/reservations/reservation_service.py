from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.config import settings
from app.services.engine.pricing_engine import QuoteResult, quote_variant

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict_loose(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}

    if isinstance(x, dict):
        return x

    if isinstance(x, (list, tuple)):
        merged: Dict[str, Any] = {}
        for item in x:
            if isinstance(item, dict):
                merged.update(item)
                continue
            if isinstance(item, str):
                s = item.strip()
                if not s:
                    continue
                try:
                    v = json.loads(s)
                except Exception:
                    continue
                if isinstance(v, dict):
                    merged.update(v)
                elif isinstance(v, (list, tuple)):
                    merged.update(_as_dict_loose(v))
        return merged

    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
        except Exception:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, (list, tuple)):
            return _as_dict_loose(v)
        return {}

    try:
        return dict(x)
    except Exception:
        return {}


def _as_decimal(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(Decimal(str(x)))
    except Exception:
        return default


def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float, Decimal)):
        return bool(x)
    s = str(x).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off", ""}:
        return False
    return default


def _as_uuid_or_none(x: Any) -> Optional[UUID]:
    if x is None:
        return None
    try:
        s = str(x).strip()
        if not s or s.lower() == "none":
            return None
        return UUID(s)
    except Exception:
        return None


def _normalize_settlement_mode(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in {"postpaid", "invoice", "bill", "billed"}:
        return "postpaid"
    if s in {"prepaid", "credit", "credits", "wallet", "payg"}:
        return "prepaid"
    if s in {"hybrid", "mixed"}:
        return "hybrid"
    return ""


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        pass
    getter = getattr(row, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def _q_money(x: Decimal) -> Decimal:
    q = Decimal("1").scaleb(-settings.MONEY_DECIMALS)
    return x.quantize(q, rounding=ROUND_HALF_UP)


def _q_pct(x: Decimal) -> Decimal:
    q = Decimal("1").scaleb(-4)
    return x.quantize(q, rounding=ROUND_HALF_UP)


def _held_credits_from_reservation(row_reserved_credits: int, quote: Dict[str, Any]) -> int:
    explicit = quote.get("reserved_hold_credits")
    if explicit not in (None, ""):
        return max(0, _as_int(explicit, 0))

    if "hold_applied" in quote:
        return max(0, row_reserved_credits) if _as_bool(quote.get("hold_applied"), False) else 0

    snapshot_mode = str(quote.get("billing_mode_snapshot") or quote.get("billing_mode") or "").strip().lower()
    settlement_mode = _normalize_settlement_mode(quote.get("settlement_mode")) or "prepaid"
    return max(0, row_reserved_credits) if (snapshot_mode == "bill" and settlement_mode == "prepaid" and row_reserved_credits > 0) else 0


def _ledger_leaf_sku_code_from_quote(quote: Dict[str, Any]) -> Optional[str]:
    """
    pricing_credit_ledger_events.sku_code has an FK to public.pricing_skus(code).

    Top-level request codes like:
      - face.creator.generate.t2i
      - face.creator.generate.i2i
    are often pricing variants / service actions, not leaf sku rows.

    For ledger rows, derive a true leaf sku from quote lines.
    If exactly one distinct line sku_code exists, use it.
    Otherwise return None and keep the requested code in metadata.
    """
    try:
        line_skus: list[str] = []
        for ln in (quote.get("lines") or []):
            d = _as_dict_loose(ln)
            s = str(d.get("sku_code") or "").strip()
            if s:
                line_skus.append(s)

        uniq = sorted(set(line_skus))
        if len(uniq) == 1:
            return uniq[0]
        return None
    except Exception:
        return None


@dataclass(frozen=True)
class BalanceView:
    balance_credits: int
    reserved_credits: int
    available_credits: int


@dataclass(frozen=True)
class ReservationView:
    reservation_id: UUID
    status: str
    reserved_credits: int
    expires_at: datetime
    currency: str
    estimated_money: Decimal
    quote: dict


@dataclass(frozen=True)
class FinalizeReceipt:
    reservation_id: UUID
    status: str
    charged_credits: int
    charged_money: Decimal
    balance_before: int
    reserved_before: int
    balance_after: int
    reserved_after: int
    available_after: int


async def _ensure_account_row(conn: asyncpg.Connection, user_id: UUID) -> None:
    await conn.execute(
        """
        insert into pricing_credit_accounts(user_id, balance_credits, reserved_credits, updated_at)
        values($1, 0, 0, now())
        on conflict (user_id) do nothing
        """,
        user_id,
    )


async def get_balance(conn: asyncpg.Connection, user_id: UUID) -> BalanceView:
    await _ensure_account_row(conn, user_id)
    r = await conn.fetchrow(
        "select balance_credits, reserved_credits from pricing_credit_accounts where user_id = $1",
        user_id,
    )
    bal = int(r["balance_credits"])
    res = int(r["reserved_credits"])
    return BalanceView(balance_credits=bal, reserved_credits=res, available_credits=max(0, bal - res))


async def _ledger_event(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    event_type: str,
    credits_delta: int,
    idempotency_key: str,
    sku_code: Optional[str] = None,
    quantity: Optional[Decimal] = None,
    unit_credits: Optional[int] = None,
    country_code: Optional[str] = None,
    currency: Optional[str] = None,
    money_amount: Optional[Decimal] = None,
    channel: Optional[str] = None,
    metadata: Optional[dict] = None,
    billing_account_id: Optional[UUID] = None,
    settlement_mode: Optional[str] = None,
    reservation_id: Optional[UUID] = None,
    studio_job_id: Optional[UUID] = None,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
) -> None:
    md = metadata or {}
    normalized_settlement_mode = _normalize_settlement_mode(settlement_mode) or None

    # First attempt: new schema with billing-account / invoice-ready fields.
    try:
        async with conn.transaction():
            await conn.execute(
                """
                insert into pricing_credit_ledger_events
                  (
                    id, user_id, billing_account_id, settlement_mode, reservation_id, studio_job_id,
                    event_type, credits_delta, sku_code, quantity, unit_credits,
                    idempotency_key, country_code, currency, money_amount, channel,
                    service_name, service_action, metadata_json, created_at
                  )
                values
                  (
                    gen_random_uuid(), $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15,
                    $16, $17, $18::jsonb, now()
                  )
                on conflict (user_id, idempotency_key) do nothing
                """,
                user_id,
                billing_account_id,
                normalized_settlement_mode,
                reservation_id,
                studio_job_id,
                event_type,
                int(credits_delta),
                sku_code,
                quantity,
                unit_credits,
                idempotency_key,
                country_code,
                currency,
                money_amount,
                channel,
                service_name,
                service_action,
                md,
            )
            return
    except Exception:
        pass

    # Fallback: older schema without extended columns.
    async with conn.transaction():
        await conn.execute(
            """
            insert into pricing_credit_ledger_events
              (id, user_id, event_type, credits_delta, sku_code, quantity, unit_credits,
               idempotency_key, country_code, currency, money_amount, channel, metadata_json, created_at)
            values (gen_random_uuid(), $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11, $12::jsonb, now())
            on conflict (user_id, idempotency_key) do nothing
            """,
            user_id,
            event_type,
            int(credits_delta),
            sku_code,
            quantity,
            unit_credits,
            idempotency_key,
            country_code,
            currency,
            money_amount,
            channel,
            md,
        )


# -------------------------
# Economics helpers
# -------------------------
async def _usd_to_currency_rate(conn: asyncpg.Connection, currency: str) -> Optional[Decimal]:
    c = (currency or "").upper()
    if c == "USD":
        return Decimal("1")

    fx = await conn.fetchrow(
        """
        select rate
        from pricing_fx_rates
        where base_currency='USD' and quote_currency=$1
        order by as_of desc
        limit 1
        """,
        c,
    )
    if fx and fx.get("rate") is not None:
        return _as_decimal(fx["rate"], "0")

    try:
        usd = await conn.fetchrow(
            """
            select money_per_credit
            from pricing_credit_value
            where currency='USD'
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc limit 1
            """
        )
        tgt = await conn.fetchrow(
            """
            select money_per_credit
            from pricing_credit_value
            where currency=$1
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc limit 1
            """,
            c,
        )
        if usd and tgt:
            usd_mpc = _as_decimal(usd["money_per_credit"], "0")
            tgt_mpc = _as_decimal(tgt["money_per_credit"], "0")
            if usd_mpc > 0:
                return tgt_mpc / usd_mpc
    except Exception:
        return None

    return None


async def _load_cost_components(conn: asyncpg.Connection, sku_codes: list[str]) -> dict:
    if not sku_codes:
        return {}

    rows = await conn.fetch(
        """
        select distinct on (sku_code, component_code)
          sku_code, component_code, cost_model, cost_currency,
          variable_cost_money, fixed_monthly_cost_money, assumed_monthly_units
        from pricing_sku_costs
        where sku_code = any($1::text[])
          and is_active = true
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by sku_code, component_code, effective_from desc
        """,
        sku_codes,
    )

    out: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(str(d["sku_code"]), []).append(d)
    return out


def _component_unit_cost_usd(comp: dict) -> Optional[Decimal]:
    cur = str(comp.get("cost_currency") or "USD").upper()
    if cur != "USD":
        return None

    model = str(comp.get("cost_model") or "blended").lower()
    var = _as_decimal(comp.get("variable_cost_money"), "0")
    fixed = _as_decimal(comp.get("fixed_monthly_cost_money"), "0")
    units = _as_decimal(comp.get("assumed_monthly_units"), "0")

    amort = Decimal("0")
    if units > 0:
        amort = fixed / units

    if model == "variable":
        return var
    if model == "amortized":
        return amort
    return var + amort


async def _compute_economics_for_quote(
    conn: asyncpg.Connection,
    *,
    currency: str,
    revenue_money_est: Decimal,
    revenue_money_shadow: Decimal,
    quote_lines: list[dict],
) -> dict:
    skus: list[str] = []
    qty_by_sku: dict[str, Decimal] = {}

    for ln in quote_lines:
        sku = str(ln.get("sku_code") or "").strip()
        if not sku:
            continue
        q_raw = ln.get("qty")
        q = q_raw if isinstance(q_raw, Decimal) else _as_decimal(q_raw, "0")
        if q <= 0:
            continue
        skus.append(sku)
        qty_by_sku[sku] = qty_by_sku.get(sku, Decimal("0")) + q

    skus = sorted(set(skus))
    if not skus:
        return {
            "has_costs_complete": False,
            "missing_cost_skus": [],
            "reason": "no_skus_in_quote",
        }

    comps_by_sku = await _load_cost_components(conn, skus)

    missing: list[str] = []
    cogs_usd = Decimal("0")

    for sku in skus:
        comps = comps_by_sku.get(sku) or []
        if not comps:
            missing.append(sku)
            continue

        unit_cost_usd = Decimal("0")
        ok = False
        for comp in comps:
            c = _component_unit_cost_usd(comp)
            if c is None:
                continue
            ok = True
            unit_cost_usd += c

        if not ok:
            missing.append(sku)
            continue

        cogs_usd += unit_cost_usd * qty_by_sku.get(sku, Decimal("0"))

    rate = await _usd_to_currency_rate(conn, currency)
    if rate is None or rate <= 0:
        return {
            "has_costs_complete": False,
            "missing_cost_skus": missing,
            "reason": "missing_fx_rate",
            "cogs_usd_partial": str(_q_money(cogs_usd)),
            "currency": currency,
        }

    cogs_money_est = _q_money(cogs_usd * rate)
    has_complete = len(missing) == 0

    def gm(rev: Decimal) -> tuple[Optional[Decimal], Optional[Decimal]]:
        if not has_complete:
            return None, None
        m = _q_money(rev - cogs_money_est)
        if rev <= 0:
            return m, None
        pct = _q_pct(m / rev)
        return m, pct

    gm_money_est, gm_pct_est = gm(_q_money(revenue_money_est))
    gm_money_shadow, gm_pct_shadow = gm(_q_money(revenue_money_shadow))

    return {
        "currency": currency,
        "has_costs_complete": has_complete,
        "missing_cost_skus": missing,
        "usd_to_currency_rate_used": str(rate),
        "cogs_money_est": str(cogs_money_est) if has_complete else None,
        "revenue_money_est": str(_q_money(revenue_money_est)),
        "gross_margin_money_est": str(gm_money_est) if gm_money_est is not None else None,
        "gross_margin_pct_est": str(gm_pct_est) if gm_pct_est is not None else None,
        "revenue_money_shadow": str(_q_money(revenue_money_shadow)),
        "gross_margin_money_shadow": str(gm_money_shadow) if gm_money_shadow is not None else None,
        "gross_margin_pct_shadow": str(gm_pct_shadow) if gm_pct_shadow is not None else None,
        "cogs_usd_total": str(_q_money(cogs_usd)),
        "computed_at": _now().isoformat(),
        "reason": "ok" if has_complete else "missing_cost_rows",
    }


# -------------------------
# Reserve / Release / Finalize
# -------------------------
async def reserve(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    idempotency_key: str,
    variant_code: str,
    params: Dict[str, Any],
    channel: str,
    country_code: str,
    currency: Optional[str],
    pricing_mode: str,
    billing_mode_snapshot: str,
    job_ref: Optional[str],
    ttl_seconds: Optional[int],
    entitlement_source: Optional[str] = None,
    entitlement_reason: Optional[str] = None,
    tier_code: Optional[str] = None,
    billing_account_id: Optional[UUID] = None,
    settlement_mode: Optional[str] = None,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
    sku_code: Optional[str] = None,
) -> ReservationView:
    ttl = ttl_seconds or settings.DEFAULT_RESERVATION_TTL_S
    ttl = max(30, min(ttl, settings.MAX_RESERVATION_TTL_S))
    expires_at = _now() + timedelta(seconds=ttl)

    existing = await conn.fetchrow(
        """
        select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json
        from pricing_credit_reservations
        where user_id = $1 and idempotency_key = $2
        """,
        user_id,
        idempotency_key,
    )
    if existing:
        q = _as_dict_loose(existing["quote_json"])
        return ReservationView(
            reservation_id=UUID(str(existing["id"])),
            status=str(existing["status"]),
            reserved_credits=int(existing["reserved_credits"] or 0),
            expires_at=existing["expires_at"],
            currency=str(existing["currency"] or q.get("currency") or ""),
            estimated_money=_as_decimal(existing["estimated_money"], "0"),
            quote=q,
        )

    quote: QuoteResult = await quote_variant(
        conn,
        user_id=user_id,
        variant_code=variant_code,
        params=params,
        channel=channel,
        country_code=country_code,
        currency=currency,
        billing_mode=pricing_mode,
    )

    quoted_credits = int(quote.total_credits)
    est_money = quote.total_money

    max_need = getattr(settings, "MAX_CREDITS_PER_RESERVATION", None)
    if isinstance(max_need, int) and max_need > 0 and quoted_credits > max_need:
        raise ValueError("PRICING_RESERVATION_TOO_LARGE")

    effective_settlement_mode = _normalize_settlement_mode(settlement_mode) or "prepaid"
    normalized_billing_mode = str((billing_mode_snapshot or pricing_mode or "")).strip().lower()

    hold_applied = bool(
        normalized_billing_mode == "bill"
        and effective_settlement_mode == "prepaid"
        and quoted_credits > 0
    )
    held_credits = quoted_credits if hold_applied else 0
    pbid = _as_uuid_or_none(getattr(quote, "pricebook_id", None))

    lines_json = [
        {
            "sku_code": l.sku_code,
            "name": l.name,
            "category": l.category,
            "provider_hint": l.provider_hint,
            "unit": l.unit,
            "qty": str(l.qty),
            "unit_credits": l.unit_credits,
            "line_credits": l.line_credits,
            "unit_money": str(l.unit_money) if l.unit_money is not None else None,
            "line_money": str(l.line_money),
        }
        for l in quote.lines
    ]

    quote_json_full: dict = {
        "pricing_engine_version": "1",
        "variant_code": quote.variant_code,
        "sku_code": sku_code or variant_code,
        "category": quote.category,
        "billing_mode": quote.billing_mode,
        "pricing_mode_used": pricing_mode,
        "billing_mode_snapshot": billing_mode_snapshot or pricing_mode,
        "entitlement_source": entitlement_source,
        "entitlement_reason": entitlement_reason,
        "tier_code": tier_code,
        "billing_account_id": str(billing_account_id) if billing_account_id else None,
        "settlement_mode": effective_settlement_mode,
        "service_name": service_name,
        "service_action": service_action,
        "hold_applied": hold_applied,
        "reserved_hold_credits": held_credits,
        "pricebook_id": str(pbid) if pbid else None,
        "pricebook_name": quote.pricebook_name,
        "currency": quote.currency,
        "total_credits": quote.total_credits,
        "total_money": str(quote.total_money),
        "shadow_total_credits": quote.shadow_total_credits,
        "shadow_total_money": str(quote.shadow_total_money) if quote.shadow_total_money is not None else None,
        "rounding_mode": quote.rounding_mode,
        "alt_currency": quote.alt_currency,
        "alt_total_money": str(quote.alt_total_money) if quote.alt_total_money is not None else None,
        "country_code": country_code or None,
        "channel": channel,
        "params": params,
        "lines": lines_json,
    }

    try:
        revenue_est = _as_decimal(str(quote.total_money), "0")
        revenue_shadow = _as_decimal(str(quote.shadow_total_money or quote.total_money), "0")
        econ = await _compute_economics_for_quote(
            conn,
            currency=quote.currency,
            revenue_money_est=revenue_est,
            revenue_money_shadow=revenue_shadow,
            quote_lines=[{"sku_code": l.sku_code, "qty": l.qty} for l in quote.lines],
        )
        quote_json_full["economics"] = econ
    except Exception:
        logger.exception("economics estimate failed; continuing without economics")
        quote_json_full["economics"] = {
            "currency": quote.currency,
            "has_costs_complete": False,
            "missing_cost_skus": [],
            "reason": "economics_estimate_failed",
            "computed_at": _now().isoformat(),
        }

    async with conn.transaction():
        await _ensure_account_row(conn, user_id)

        try:
            async with conn.transaction():
                await conn.execute(
                    """
                    update pricing_credit_accounts
                    set
                      billing_account_id = coalesce(billing_account_id, $2),
                      settlement_mode = $3,
                      updated_at = now()
                    where user_id = $1
                    """,
                    user_id,
                    billing_account_id,
                    effective_settlement_mode,
                )
        except Exception:
            pass

        acc = await conn.fetchrow(
            "select balance_credits, reserved_credits from pricing_credit_accounts where user_id = $1 for update",
            user_id,
        )
        bal = int(acc["balance_credits"])
        res = int(acc["reserved_credits"])
        avail = bal - res

        if held_credits > 0 and avail < held_credits:
            raise ValueError("PRICING_INSUFFICIENT_CREDITS")

        try:
            async with conn.transaction():
                rid_row = await conn.fetchrow(
                    """
                    insert into pricing_credit_reservations
                      (
                        id, user_id, billing_account_id, settlement_mode, status,
                        pricebook_id, country_code, currency, channel, tier_code,
                        service_name, service_action, sku_code,
                        quote_json, reserved_credits, estimated_money, idempotency_key, job_ref,
                        expires_at, finalized_at, created_at, updated_at
                      )
                    values
                      (
                        gen_random_uuid(), $1, $2, $3, 'reserved',
                        $4, $5, $6, $7, $8,
                        $9, $10, $11,
                        $12::jsonb, $13, $14, $15, $16,
                        $17, null, now(), now()
                      )
                    on conflict (user_id, idempotency_key) do nothing
                    returning id
                    """,
                    user_id,
                    billing_account_id,
                    effective_settlement_mode,
                    pbid,
                    country_code or None,
                    quote.currency,
                    channel,
                    tier_code,
                    service_name,
                    service_action,
                    sku_code or variant_code,
                    quote_json_full,
                    held_credits,
                    est_money,
                    idempotency_key,
                    job_ref,
                    expires_at,
                )
        except Exception:
            async with conn.transaction():
                rid_row = await conn.fetchrow(
                    """
                    insert into pricing_credit_reservations
                      (id, user_id, status, pricebook_id, country_code, currency, channel, tier_code,
                       quote_json, reserved_credits, estimated_money, idempotency_key, job_ref,
                       expires_at, finalized_at, created_at, updated_at)
                    values
                      (gen_random_uuid(), $1, 'reserved', $2, $3, $4, $5, $6,
                       $7::jsonb, $8, $9, $10, $11,
                       $12, null, now(), now())
                    on conflict (user_id, idempotency_key) do nothing
                    returning id
                    """,
                    user_id,
                    pbid,
                    country_code or None,
                    quote.currency,
                    channel,
                    tier_code,
                    quote_json_full,
                    held_credits,
                    est_money,
                    idempotency_key,
                    job_ref,
                    expires_at,
                )

        if not rid_row:
            existing2 = await conn.fetchrow(
                """
                select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json
                from pricing_credit_reservations
                where user_id = $1 and idempotency_key = $2
                """,
                user_id,
                idempotency_key,
            )
            if not existing2:
                raise RuntimeError("PRICING_RESERVATION_RACE_FETCH_FAILED")
            q2 = _as_dict_loose(existing2["quote_json"])
            return ReservationView(
                reservation_id=UUID(str(existing2["id"])),
                status=str(existing2["status"]),
                reserved_credits=int(existing2["reserved_credits"] or 0),
                expires_at=existing2["expires_at"],
                currency=str(existing2["currency"] or q2.get("currency") or ""),
                estimated_money=_as_decimal(existing2["estimated_money"], "0"),
                quote=q2,
            )

        rid = UUID(str(rid_row["id"]))

        if held_credits > 0:
            await conn.execute(
                """
                update pricing_credit_accounts
                set reserved_credits = reserved_credits + $2,
                    updated_at = now()
                where user_id = $1
                """,
                user_id,
                held_credits,
            )

        ledger_sku_code = _ledger_leaf_sku_code_from_quote(quote_json_full)

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_hold",
            credits_delta=0,
            idempotency_key=f"reserve_hold:{idempotency_key}",
            sku_code=ledger_sku_code,
            quantity=_as_decimal(params.get("requested_units"), "0"),
            country_code=country_code or None,
            currency=quote.currency,
            channel=channel,
            money_amount=est_money,
            metadata={
                "reservation_id": str(rid),
                "variant_code": variant_code,
                "requested_code": sku_code or variant_code,
                "ledger_sku_code": ledger_sku_code,
                "billing_mode_snapshot": billing_mode_snapshot or pricing_mode,
                "settlement_mode": effective_settlement_mode,
                "hold_applied": hold_applied,
                "quoted_credits": quoted_credits,
                "held_credits": held_credits,
                "reserved_delta_applied": held_credits,
                "reserved_delta_intended": held_credits,
                "balance_before": bal,
                "reserved_before": res,
                "available_before": avail,
                "reserved_after": (res + held_credits),
            },
            billing_account_id=billing_account_id,
            settlement_mode=effective_settlement_mode,
            reservation_id=rid,
            studio_job_id=_as_uuid_or_none(job_ref),
            service_name=service_name,
            service_action=service_action,
        )

    return ReservationView(
        reservation_id=rid,
        status="reserved",
        reserved_credits=held_credits,
        expires_at=expires_at,
        currency=quote.currency,
        estimated_money=est_money,
        quote=quote_json_full,
    )


async def release(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    reservation_id: Optional[UUID],
    idempotency_key: Optional[str],
    channel: str,
    country_code: str,
    reason: str,
) -> ReservationView:
    if not reservation_id and not idempotency_key:
        raise ValueError("PRICING_RELEASE_REQUIRES_RESERVATION")

    async with conn.transaction():
        await _ensure_account_row(conn, user_id)

        if reservation_id:
            r = await conn.fetchrow(
                """
                select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json
                from pricing_credit_reservations
                where user_id = $1 and id = $2
                for update
                """,
                user_id,
                reservation_id,
            )
        else:
            r = await conn.fetchrow(
                """
                select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json
                from pricing_credit_reservations
                where user_id = $1 and idempotency_key = $2
                for update
                """,
                user_id,
                idempotency_key,
            )
        if not r:
            raise ValueError("PRICING_RESERVATION_NOT_FOUND")

        rid = UUID(str(r["id"]))
        st = str(r["status"])
        row_reserved = int(r["reserved_credits"] or 0)
        quote = _as_dict_loose(r["quote_json"])
        currency = str(r["currency"] or quote.get("currency") or "")

        held_effective = _held_credits_from_reservation(row_reserved, quote)
        hold_applied = held_effective > 0

        if st in {"released", "expired", "cancelled", "failed"}:
            return ReservationView(
                reservation_id=rid,
                status=st,
                reserved_credits=0,
                expires_at=r["expires_at"],
                currency=currency,
                estimated_money=_as_decimal(r["estimated_money"], "0"),
                quote=quote,
            )

        if st == "committed":
            raise ValueError("PRICING_RESERVATION_ALREADY_COMMITTED")

        acc = await conn.fetchrow(
            "select balance_credits, reserved_credits from pricing_credit_accounts where user_id=$1 for update",
            user_id,
        )
        bal = int(acc["balance_credits"])
        res = int(acc["reserved_credits"])

        new_res = res
        if held_effective > 0:
            new_res = max(0, res - held_effective)

        await conn.execute(
            "update pricing_credit_accounts set reserved_credits=$2, updated_at=now() where user_id=$1",
            user_id,
            new_res,
        )
        await conn.execute(
            """
            update pricing_credit_reservations
            set status='released', finalized_at=coalesce(finalized_at, now()), updated_at=now()
            where user_id=$1 and id=$2
            """,
            user_id,
            rid,
        )

        ledger_sku_code = _ledger_leaf_sku_code_from_quote(quote)

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_release",
            credits_delta=0,
            idempotency_key=f"reserve_release:{rid}",
            sku_code=ledger_sku_code,
            country_code=country_code or None,
            currency=currency or None,
            channel=channel,
            metadata={
                "reservation_id": str(rid),
                "reason": reason,
                "requested_code": str(quote.get("sku_code") or quote.get("variant_code") or "") or None,
                "ledger_sku_code": ledger_sku_code,
                "settlement_mode": _normalize_settlement_mode(quote.get("settlement_mode")) or "prepaid",
                "hold_applied": hold_applied,
                "held_credits": held_effective,
                "reserved_delta_applied": (-held_effective) if hold_applied else 0,
                "reserved_delta_intended": (-held_effective) if held_effective > 0 else 0,
                "balance_before": bal,
                "reserved_before": res,
                "reserved_after": new_res,
            },
            billing_account_id=_as_uuid_or_none(quote.get("billing_account_id")),
            settlement_mode=_normalize_settlement_mode(quote.get("settlement_mode")) or "prepaid",
            reservation_id=rid,
            studio_job_id=_as_uuid_or_none(_as_dict_loose(quote.get("params")).get("external_ref_id")),
            service_name=str(quote.get("service_name") or "") or None,
            service_action=str(quote.get("service_action") or "") or None,
        )

        return ReservationView(
            reservation_id=rid,
            status="released",
            reserved_credits=0,
            expires_at=r["expires_at"],
            currency=currency,
            estimated_money=_as_decimal(r["estimated_money"], "0"),
            quote=quote,
        )


async def finalize(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    reservation_id: UUID,
    finalize_idempotency_key: str,
    actuals: Dict[str, Any],
    channel: str,
    country_code: str,
    billing_mode: str,
    billing_account_id: Optional[UUID] = None,
    settlement_mode: Optional[str] = None,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
    sku_code: Optional[str] = None,
    studio_job_id: Optional[UUID] = None,
) -> FinalizeReceipt:
    async with conn.transaction():
        await _ensure_account_row(conn, user_id)

        try:
            r = await conn.fetchrow(
                """
                select
                  id, status, reserved_credits, currency, estimated_money, quote_json,
                  billing_account_id, settlement_mode, service_name, service_action, sku_code
                from pricing_credit_reservations
                where user_id=$1 and id=$2
                for update
                """,
                user_id,
                reservation_id,
            )
        except Exception:
            r = await conn.fetchrow(
                """
                select id, status, reserved_credits, currency, estimated_money, quote_json
                from pricing_credit_reservations
                where user_id=$1 and id=$2
                for update
                """,
                user_id,
                reservation_id,
            )
        if not r:
            raise ValueError("PRICING_RESERVATION_NOT_FOUND")

        st = str(r["status"])
        row_reserved = int(r["reserved_credits"] or 0)
        quote = _as_dict_loose(r["quote_json"])
        currency = str(r["currency"] or quote.get("currency") or "")

        effective_billing_account_id = (
            _as_uuid_or_none(_row_get(r, "billing_account_id"))
            or _as_uuid_or_none(quote.get("billing_account_id"))
            or billing_account_id
        )
        effective_settlement_mode = (
            _normalize_settlement_mode(_row_get(r, "settlement_mode"))
            or _normalize_settlement_mode(quote.get("settlement_mode"))
            or _normalize_settlement_mode(settlement_mode)
            or "prepaid"
        )
        effective_service_name = (
            str(_row_get(r, "service_name") or "").strip()
            or str(quote.get("service_name") or "").strip()
            or str(service_name or "").strip()
            or None
        )
        effective_service_action = (
            str(_row_get(r, "service_action") or "").strip()
            or str(quote.get("service_action") or "").strip()
            or str(service_action or "").strip()
            or None
        )
        effective_sku_code = (
            str(_row_get(r, "sku_code") or "").strip()
            or str(quote.get("sku_code") or quote.get("variant_code") or "").strip()
            or str(sku_code or "").strip()
            or None
        )
        effective_job_id = (
            studio_job_id
            or _as_uuid_or_none(_as_dict_loose(quote.get("params")).get("external_ref_id"))
            or _as_uuid_or_none(actuals.get("external_ref_id"))
        )

        acc = await conn.fetchrow(
            "select balance_credits, reserved_credits from pricing_credit_accounts where user_id=$1 for update",
            user_id,
        )
        bal_before = int(acc["balance_credits"])
        res_before = int(acc["reserved_credits"])

        if st == "committed":
            avail_after = max(0, bal_before - res_before)
            return FinalizeReceipt(
                reservation_id=reservation_id,
                status="committed",
                charged_credits=_as_int(quote.get("final_charged_credits"), 0),
                charged_money=_as_decimal(quote.get("final_charged_money"), "0"),
                balance_before=bal_before,
                reserved_before=res_before,
                balance_after=bal_before,
                reserved_after=res_before,
                available_after=avail_after,
            )

        if st != "reserved":
            raise ValueError(f"PRICING_INVALID_RESERVATION_STATUS:{st}")

        snapshot_mode = str(quote.get("billing_mode_snapshot") or quote.get("billing_mode") or "").strip()
        effective_billing_mode = snapshot_mode if snapshot_mode else str(billing_mode or "bill")

        held_effective = _held_credits_from_reservation(row_reserved, quote)
        hold_applied = held_effective > 0

        quoted_credits = _as_int(quote.get("total_credits"), 0)
        quoted_money = _as_decimal(quote.get("total_money"), "0")

        if effective_billing_mode in {"shadow", "free", "disabled", "included"}:
            final_credits = 0
            final_money = Decimal("0")
        elif effective_billing_mode == "bill":
            if effective_settlement_mode == "postpaid":
                final_credits = 0
                final_money = quoted_money
            else:
                final_credits = quoted_credits
                final_money = quoted_money
        else:
            final_credits = quoted_credits
            final_money = quoted_money

        available_effective = bal_before - res_before + held_effective
        if final_credits > 0 and available_effective < final_credits:
            raise ValueError("PRICING_INSUFFICIENT_CREDITS_OVERAGE")

        new_reserved = res_before
        if held_effective > 0:
            new_reserved = max(0, res_before - held_effective)

        new_balance = bal_before - final_credits
        if new_balance < 0:
            raise ValueError("PRICING_NEGATIVE_BALANCE_GUARD")

        await conn.execute(
            """
            update pricing_credit_accounts
            set balance_credits=$2, reserved_credits=$3, updated_at=now()
            where user_id=$1
            """,
            user_id,
            new_balance,
            new_reserved,
        )

        try:
            econ = _as_dict_loose(quote.get("economics"))
            if econ:
                econ["revenue_money_final"] = str(_q_money(final_money))
                q_lines = []
                for ln in (quote.get("lines") or []):
                    d = _as_dict_loose(ln)
                    q_lines.append({"sku_code": d.get("sku_code"), "qty": d.get("qty")})
                econ2 = await _compute_economics_for_quote(
                    conn,
                    currency=currency,
                    revenue_money_est=_as_decimal(econ.get("revenue_money_est"), "0"),
                    revenue_money_shadow=_as_decimal(econ.get("revenue_money_shadow"), "0"),
                    quote_lines=q_lines,
                )
                if econ2.get("has_costs_complete"):
                    cogs_est = _as_decimal(econ2.get("cogs_money_est"), "0")
                    econ["cogs_money_final"] = str(cogs_est)
                    econ["gross_margin_money_final"] = str(_q_money(final_money - cogs_est))
                    econ["gross_margin_pct_final"] = (
                        str(_q_pct((_q_money(final_money - cogs_est) / _q_money(final_money))))
                        if final_money > 0
                        else None
                    )
                else:
                    econ["cogs_money_final"] = None
                    econ["gross_margin_money_final"] = None
                    econ["gross_margin_pct_final"] = None
                    econ["has_costs_complete"] = False
                    econ["missing_cost_skus"] = econ2.get("missing_cost_skus") or econ.get("missing_cost_skus") or []
                    econ["reason"] = econ2.get("reason") or econ.get("reason") or "missing_cost_rows"
                econ["computed_at_final"] = _now().isoformat()
                quote["economics"] = econ
        except Exception:
            logger.exception("economics finalize failed; continuing without economics final fields")

        quote["finalize"] = {
            "finalize_idempotency_key": finalize_idempotency_key,
            "billing_mode_effective": effective_billing_mode,
            "settlement_mode_effective": effective_settlement_mode,
            "billing_mode_param": billing_mode,
            "billing_mode_snapshot": snapshot_mode,
            "actuals": actuals,
            "final_charged_credits": final_credits,
            "final_charged_money": str(final_money),
            "held_credits_row": row_reserved,
            "held_credits_effective": held_effective,
            "hold_applied": hold_applied,
            "timestamp": _now().isoformat(),
        }
        quote["final_charged_credits"] = final_credits
        quote["final_charged_money"] = str(final_money)
        quote["reserved_hold_credits"] = held_effective
        quote["billing_account_id"] = (
            str(effective_billing_account_id) if effective_billing_account_id else quote.get("billing_account_id")
        )
        quote["settlement_mode"] = effective_settlement_mode
        if effective_service_name:
            quote["service_name"] = effective_service_name
        if effective_service_action:
            quote["service_action"] = effective_service_action
        if effective_sku_code:
            quote["sku_code"] = effective_sku_code

        try:
            async with conn.transaction():
                await conn.execute(
                    """
                    update pricing_credit_reservations
                    set
                      status='committed',
                      finalized_at=now(),
                      updated_at=now(),
                      billing_account_id=coalesce(billing_account_id, $3),
                      settlement_mode=$4,
                      service_name=coalesce(service_name, $5),
                      service_action=coalesce(service_action, $6),
                      sku_code=coalesce(sku_code, $7),
                      quote_json=$8::jsonb
                    where user_id=$1 and id=$2
                    """,
                    user_id,
                    reservation_id,
                    effective_billing_account_id,
                    effective_settlement_mode,
                    effective_service_name,
                    effective_service_action,
                    effective_sku_code,
                    quote,
                )
        except Exception:
            async with conn.transaction():
                await conn.execute(
                    """
                    update pricing_credit_reservations
                    set status='committed', finalized_at=now(), updated_at=now(), quote_json=$3::jsonb
                    where user_id=$1 and id=$2
                    """,
                    user_id,
                    reservation_id,
                    quote,
                )

        ledger_sku_code = _ledger_leaf_sku_code_from_quote(quote)

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="consume",
            credits_delta=-final_credits,
            idempotency_key=f"consume:{finalize_idempotency_key}",
            sku_code=ledger_sku_code,
            quantity=_as_decimal(actuals.get("actual_units"), "0"),
            country_code=country_code or None,
            currency=currency or None,
            channel=channel,
            money_amount=final_money,
            metadata={
                "reservation_id": str(reservation_id),
                "variant_code": quote.get("variant_code"),
                "requested_code": effective_sku_code,
                "ledger_sku_code": ledger_sku_code,
                "billing_mode_effective": effective_billing_mode,
                "settlement_mode_effective": effective_settlement_mode,
                "hold_applied": hold_applied,
                "held_credits": held_effective,
                "charged_credits": final_credits,
                "balance_before": bal_before,
                "reserved_before": res_before,
                "balance_after": new_balance,
                "reserved_after": new_reserved,
            },
            billing_account_id=effective_billing_account_id,
            settlement_mode=effective_settlement_mode,
            reservation_id=reservation_id,
            studio_job_id=effective_job_id,
            service_name=effective_service_name,
            service_action=effective_service_action,
        )

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_release",
            credits_delta=0,
            idempotency_key=f"reserve_release_finalize:{finalize_idempotency_key}",
            sku_code=ledger_sku_code,
            country_code=country_code or None,
            currency=currency or None,
            channel=channel,
            metadata={
                "reservation_id": str(reservation_id),
                "reason": "finalize",
                "requested_code": effective_sku_code,
                "ledger_sku_code": ledger_sku_code,
                "settlement_mode_effective": effective_settlement_mode,
                "hold_applied": hold_applied,
                "held_credits": held_effective,
                "reserved_delta_applied": (-held_effective) if hold_applied else 0,
                "reserved_delta_intended": (-held_effective) if held_effective > 0 else 0,
                "balance_before": bal_before,
                "reserved_before": res_before,
                "balance_after": new_balance,
                "reserved_after": new_reserved,
            },
            billing_account_id=effective_billing_account_id,
            settlement_mode=effective_settlement_mode,
            reservation_id=reservation_id,
            studio_job_id=effective_job_id,
            service_name=effective_service_name,
            service_action=effective_service_action,
        )

        avail_after = max(0, new_balance - new_reserved)

        return FinalizeReceipt(
            reservation_id=reservation_id,
            status="committed",
            charged_credits=final_credits,
            charged_money=final_money,
            balance_before=bal_before,
            reserved_before=res_before,
            balance_after=new_balance,
            reserved_after=new_reserved,
            available_after=avail_after,
        )