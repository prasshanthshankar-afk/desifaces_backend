# services/svc-pricing/app/app/services/reservations/reservation_service.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.config import settings
from app.services.engine.pricing_engine import QuoteResult, quote_variant

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
) -> None:
    md = metadata or {}
    await conn.execute(
        """
        insert into pricing_credit_ledger_events
          (id, user_id, event_type, credits_delta, sku_code, quantity, unit_credits,
           idempotency_key, country_code, currency, money_amount, channel, metadata_json, created_at)
        values (gen_random_uuid(), $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11, $12::jsonb, now())
        on conflict (user_id, idempotency_key) do nothing
        """,
        user_id, event_type, int(credits_delta), sku_code,
        quantity, unit_credits,
        idempotency_key, country_code, currency,
        money_amount, channel,
        md,
    )


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
    billing_mode: str,         # from module_gate
    job_ref: Optional[str],
    ttl_seconds: Optional[int],
) -> ReservationView:
    ttl = ttl_seconds or settings.DEFAULT_RESERVATION_TTL_S
    ttl = max(30, min(ttl, settings.MAX_RESERVATION_TTL_S))
    expires_at = _now() + timedelta(seconds=ttl)

    # Idempotency: if reservation exists, return it.
    existing = await conn.fetchrow(
        """
        select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json
        from pricing_credit_reservations
        where user_id = $1 and idempotency_key = $2
        """,
        user_id, idempotency_key,
    )
    if existing:
        return ReservationView(
            reservation_id=UUID(str(existing["id"])),
            status=str(existing["status"]),
            reserved_credits=int(existing["reserved_credits"]),
            expires_at=existing["expires_at"],
            currency=str(existing["currency"] or ""),
            estimated_money=Decimal(str(existing["estimated_money"] or "0")),
            quote=dict(existing["quote_json"] or {}),
        )

    # Compute quote server-side (never trust client for credits)
    quote: QuoteResult = await quote_variant(
        conn,
        user_id=user_id,
        variant_code=variant_code,
        params=params,
        channel=channel,
        country_code=country_code,
        currency=currency,
        billing_mode=billing_mode,
    )

    need = int(quote.total_credits)
    est_money = quote.total_money

    async with conn.transaction():
        await _ensure_account_row(conn, user_id)

        # Lock account row for atomic reserved accounting
        acc = await conn.fetchrow(
            "select balance_credits, reserved_credits from pricing_credit_accounts where user_id = $1 for update",
            user_id,
        )
        bal = int(acc["balance_credits"])
        res = int(acc["reserved_credits"])
        avail = bal - res

        if billing_mode == "bill" and need > 0 and avail < need:
            raise ValueError("PRICING_INSUFFICIENT_CREDITS")

        # Insert reservation row
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
            returning id
            """,
            user_id,
            str(quote.pricebook_id),
            country_code or None,
            quote.currency,
            channel,
            None,  # tier_code is optional in schema; we can embed in quote_json if needed
            {
                "variant_code": quote.variant_code,
                "category": quote.category,
                "billing_mode": quote.billing_mode,
                "pricebook_id": str(quote.pricebook_id),
                "pricebook_name": quote.pricebook_name,
                "currency": quote.currency,
                "total_credits": quote.total_credits,
                "total_money": str(quote.total_money),
                "rounding_mode": quote.rounding_mode,
                "alt_currency": quote.alt_currency,
                "alt_total_money": str(quote.alt_total_money) if quote.alt_total_money is not None else None,
                "params": params,
                "lines": [
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
                ],
            },
            need,
            est_money,
            idempotency_key,
            job_ref,
            expires_at,
        )
        rid = UUID(str(rid_row["id"]))

        # Update account reserved_credits for bill mode only (shadow/free reserve 0)
        if billing_mode == "bill" and need > 0:
            await conn.execute(
                """
                update pricing_credit_accounts
                set reserved_credits = reserved_credits + $2,
                    updated_at = now()
                where user_id = $1
                """,
                user_id, need,
            )

        # Ledger: reserve_hold (credits_delta=0; reserved movement in metadata)
        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_hold",
            credits_delta=0,
            idempotency_key=f"reserve_hold:{idempotency_key}",
            country_code=country_code or None,
            currency=quote.currency,
            channel=channel,
            money_amount=est_money,
            metadata={
                "reservation_id": str(rid),
                "variant_code": variant_code,
                "billing_mode": billing_mode,
                "reserved_delta": need,
                "balance_before": bal,
                "reserved_before": res,
                "available_before": avail,
            },
        )

    return ReservationView(
        reservation_id=rid,
        status="reserved",
        reserved_credits=need,
        expires_at=expires_at,
        currency=quote.currency,
        estimated_money=est_money,
        quote={
            "variant_code": quote.variant_code,
            "category": quote.category,
            "billing_mode": quote.billing_mode,
            "pricebook_id": str(quote.pricebook_id),
            "pricebook_name": quote.pricebook_name,
            "currency": quote.currency,
            "total_credits": quote.total_credits,
            "total_money": str(quote.total_money),
        },
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
                user_id, reservation_id,
            )
        else:
            r = await conn.fetchrow(
                """
                select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json
                from pricing_credit_reservations
                where user_id = $1 and idempotency_key = $2
                for update
                """,
                user_id, idempotency_key,
            )
        if not r:
            raise ValueError("PRICING_RESERVATION_NOT_FOUND")

        rid = UUID(str(r["id"]))
        st = str(r["status"])
        held = int(r["reserved_credits"])

        if st in {"released", "expired"}:
            # idempotent no-op
            return ReservationView(
                reservation_id=rid,
                status=st,
                reserved_credits=held,
                expires_at=r["expires_at"],
                currency=str(r["currency"] or ""),
                estimated_money=Decimal(str(r["estimated_money"] or "0")),
                quote=dict(r["quote_json"] or {}),
            )

        if st == "finalized":
            return ReservationView(
                reservation_id=rid,
                status=st,
                reserved_credits=0,
                expires_at=r["expires_at"],
                currency=str(r["currency"] or ""),
                estimated_money=Decimal(str(r["estimated_money"] or "0")),
                quote=dict(r["quote_json"] or {}),
            )

        # Lock account
        acc = await conn.fetchrow(
            "select balance_credits, reserved_credits from pricing_credit_accounts where user_id=$1 for update",
            user_id,
        )
        bal = int(acc["balance_credits"])
        res = int(acc["reserved_credits"])
        new_res = max(0, res - held)

        await conn.execute(
            "update pricing_credit_accounts set reserved_credits=$2, updated_at=now() where user_id=$1",
            user_id, new_res,
        )
        await conn.execute(
            """
            update pricing_credit_reservations
            set status='released', updated_at=now()
            where user_id=$1 and id=$2
            """,
            user_id, rid,
        )

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_release",
            credits_delta=0,
            idempotency_key=f"reserve_release:{rid}",
            country_code=country_code or None,
            currency=str(r["currency"] or None),
            channel=channel,
            metadata={
                "reservation_id": str(rid),
                "reason": reason,
                "reserved_delta": -held,
                "balance_before": bal,
                "reserved_before": res,
                "reserved_after": new_res,
            },
        )

        return ReservationView(
            reservation_id=rid,
            status="released",
            reserved_credits=0,
            expires_at=r["expires_at"],
            currency=str(r["currency"] or ""),
            estimated_money=Decimal(str(r["estimated_money"] or "0")),
            quote=dict(r["quote_json"] or {}),
        )


async def finalize(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    reservation_id: UUID,
    finalize_idempotency_key: str,
    actuals: Dict[str, Any],      # optional metering inputs (future)
    channel: str,
    country_code: str,
    billing_mode: str,            # from module_gate (re-evaluated at finalize time)
) -> FinalizeReceipt:
    """
    Finalize deducts balance_credits, releases reserved_credits, and writes ledger.
    We recompute final charge from the stored quote for now (no metered SKU lines yet).
    Later, metered lines can be recomputed using `actuals`.
    """
    async with conn.transaction():
        await _ensure_account_row(conn, user_id)

        r = await conn.fetchrow(
            """
            select id, status, reserved_credits, currency, estimated_money, quote_json
            from pricing_credit_reservations
            where user_id=$1 and id=$2
            for update
            """,
            user_id, reservation_id,
        )
        if not r:
            raise ValueError("PRICING_RESERVATION_NOT_FOUND")

        st = str(r["status"])
        held = int(r["reserved_credits"])
        currency = str(r["currency"] or "")
        quote = dict(r["quote_json"] or {})

        # Idempotent: if already finalized, return receipt-like view from current account
        acc = await conn.fetchrow(
            "select balance_credits, reserved_credits from pricing_credit_accounts where user_id=$1 for update",
            user_id,
        )
        bal_before = int(acc["balance_credits"])
        res_before = int(acc["reserved_credits"])

        if st == "finalized":
            avail_after = max(0, bal_before - res_before)
            return FinalizeReceipt(
                reservation_id=reservation_id,
                status="finalized",
                charged_credits=int(quote.get("final_charged_credits") or 0),
                charged_money=Decimal(str(quote.get("final_charged_money") or "0")),
                balance_before=bal_before,
                reserved_before=res_before,
                balance_after=bal_before,
                reserved_after=res_before,
                available_after=avail_after,
            )

        if st not in {"reserved"}:
            raise ValueError(f"PRICING_INVALID_RESERVATION_STATUS:{st}")

        # Determine final charge:
        # For now: charge equals quoted credits unless shadow/free.
        quoted_credits = int(quote.get("total_credits") or 0)
        quoted_money = Decimal(str(quote.get("total_money") or "0"))

        if billing_mode in {"shadow", "free"}:
            final_credits = 0
            final_money = Decimal("0")
        else:
            final_credits = quoted_credits
            final_money = quoted_money

        # Ensure any overage (final>held) is affordable
        bal = bal_before
        res = res_before
        avail = bal - res
        extra_needed = max(0, final_credits - held)

        if billing_mode == "bill" and extra_needed > 0 and avail < extra_needed:
            raise ValueError("PRICING_INSUFFICIENT_CREDITS_OVERAGE")

        # Apply accounting:
        # - release hold: reserved -= held
        # - consume: balance -= final
        new_reserved = max(0, res - held)
        new_balance = bal - final_credits
        if new_balance < 0:
            # Should never happen with checks, but guard.
            raise ValueError("PRICING_NEGATIVE_BALANCE_GUARD")

        await conn.execute(
            """
            update pricing_credit_accounts
            set balance_credits=$2, reserved_credits=$3, updated_at=now()
            where user_id=$1
            """,
            user_id, new_balance, new_reserved,
        )

        # Mark finalized; store finalize info inside quote_json for audit
        quote["finalize"] = {
            "finalize_idempotency_key": finalize_idempotency_key,
            "billing_mode_at_finalize": billing_mode,
            "actuals": actuals,
            "final_charged_credits": final_credits,
            "final_charged_money": str(final_money),
            "held_credits": held,
            "timestamp": _now().isoformat(),
        }
        quote["final_charged_credits"] = final_credits
        quote["final_charged_money"] = str(final_money)

        await conn.execute(
            """
            update pricing_credit_reservations
            set status='finalized', finalized_at=now(), updated_at=now(), quote_json=$3::jsonb
            where user_id=$1 and id=$2
            """,
            user_id, reservation_id, quote,
        )

        # Ledger: consume (negative credits_delta)
        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="consume",
            credits_delta=-final_credits,
            idempotency_key=f"consume:{finalize_idempotency_key}",
            country_code=country_code or None,
            currency=currency or None,
            channel=channel,
            money_amount=final_money,
            metadata={
                "reservation_id": str(reservation_id),
                "variant_code": quote.get("variant_code"),
                "billing_mode": billing_mode,
                "held_credits": held,
                "charged_credits": final_credits,
                "balance_before": bal_before,
                "reserved_before": res_before,
                "balance_after": new_balance,
                "reserved_after": new_reserved,
            },
        )

        # Ledger: reserve_release (audit)
        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_release",
            credits_delta=0,
            idempotency_key=f"reserve_release_finalize:{finalize_idempotency_key}",
            country_code=country_code or None,
            currency=currency or None,
            channel=channel,
            metadata={
                "reservation_id": str(reservation_id),
                "reserved_delta": -held,
                "reason": "finalize",
            },
        )

        avail_after = max(0, new_balance - new_reserved)

        return FinalizeReceipt(
            reservation_id=reservation_id,
            status="finalized",
            charged_credits=final_credits,
            charged_money=final_money,
            balance_before=bal_before,
            reserved_before=res_before,
            balance_after=new_balance,
            reserved_after=new_reserved,
            available_after=avail_after,
        )