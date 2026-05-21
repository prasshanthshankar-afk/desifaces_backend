from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from uuid import UUID


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return default


@dataclass(frozen=True)
class PlanCreditGuardrail:
    plan_code: str
    tier_code: Optional[str]
    included_credit_cap: Optional[Decimal]
    wallet_credit_cap: Optional[Decimal]
    enforce_wallet_cap: bool
    allow_topups: bool
    is_active: bool

    def as_public_dict(self) -> Dict[str, Any]:
        return {
            "plan_code": self.plan_code,
            "tier_code": self.tier_code,
            "included_credit_cap": str(self.included_credit_cap) if self.included_credit_cap is not None else None,
            "wallet_credit_cap": str(self.wallet_credit_cap) if self.wallet_credit_cap is not None else None,
            "enforce_wallet_cap": self.enforce_wallet_cap,
            "allow_topups": self.allow_topups,
            "is_active": self.is_active,
        }


class TopupGuardrailError(Exception):
    def __init__(self, code: str, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(code)
        self.code = code
        self.context = context or {}


async def fetch_plan_credit_guardrail(conn, *, plan_code: str) -> Optional[PlanCreditGuardrail]:
    row = await conn.fetchrow(
        """
        select
          plan_code,
          tier_code,
          included_credit_cap,
          wallet_credit_cap,
          enforce_wallet_cap,
          allow_topups,
          is_active
        from public.pricing_plan_credit_guardrails
        where plan_code = $1
          and is_active = true
        limit 1
        """,
        str(plan_code or "free").strip().lower() or "free",
    )
    if not row:
        return None

    return PlanCreditGuardrail(
        plan_code=str(row["plan_code"] or "free"),
        tier_code=str(row["tier_code"]) if row["tier_code"] is not None else None,
        included_credit_cap=_to_decimal(row["included_credit_cap"]) if row["included_credit_cap"] is not None else None,
        wallet_credit_cap=_to_decimal(row["wallet_credit_cap"]) if row["wallet_credit_cap"] is not None else None,
        enforce_wallet_cap=_to_bool(row["enforce_wallet_cap"], False),
        allow_topups=_to_bool(row["allow_topups"], True),
        is_active=_to_bool(row["is_active"], True),
    )


async def fetch_purchased_wallet_totals(conn, *, user_id: UUID) -> Dict[str, Decimal]:
    row = await conn.fetchrow(
        """
        select
          coalesce(sum(remaining_amount), 0)::numeric as purchased_available,
          coalesce(sum(reserved_amount), 0)::numeric as purchased_reserved,
          coalesce(sum(remaining_amount + reserved_amount), 0)::numeric as purchased_total
        from public.pricing_credit_lots
        where user_id = $1
          and status = 'active'
          and bucket_type = 'purchased'
        """,
        user_id,
    )
    if not row:
        return {
            "purchased_available": Decimal("0"),
            "purchased_reserved": Decimal("0"),
            "purchased_total": Decimal("0"),
        }

    return {
        "purchased_available": _to_decimal(row["purchased_available"]),
        "purchased_reserved": _to_decimal(row["purchased_reserved"]),
        "purchased_total": _to_decimal(row["purchased_total"]),
    }


async def validate_wallet_topup_allowed(
    conn,
    *,
    user_id: UUID,
    plan_code: str,
    credits_to_grant: Any,
) -> Dict[str, Any]:
    guardrail = await fetch_plan_credit_guardrail(conn, plan_code=plan_code)
    guardrail_public = guardrail.as_public_dict() if guardrail else {
        "plan_code": str(plan_code or "free").strip().lower() or "free",
        "allow_topups": True,
        "enforce_wallet_cap": False,
    }

    if guardrail and not guardrail.allow_topups:
        raise TopupGuardrailError(
            "topups_not_allowed_for_plan",
            {"plan_code": guardrail.plan_code, "guardrails": guardrail_public},
        )

    wallet_totals = await fetch_purchased_wallet_totals(conn, user_id=user_id)
    grant_amount = _to_decimal(credits_to_grant)
    projected_wallet_total = wallet_totals["purchased_total"] + grant_amount

    if (
        guardrail
        and guardrail.enforce_wallet_cap
        and guardrail.wallet_credit_cap is not None
        and projected_wallet_total > guardrail.wallet_credit_cap
    ):
        raise TopupGuardrailError(
            "wallet_credit_cap_exceeded",
            {
                "plan_code": guardrail.plan_code,
                "wallet_credit_cap": str(guardrail.wallet_credit_cap),
                "current_purchased_wallet_total": str(wallet_totals["purchased_total"]),
                "credits_to_grant": str(grant_amount),
                "projected_wallet_total": str(projected_wallet_total),
                "guardrails": guardrail_public,
            },
        )

    return {
        "plan_code": str(plan_code or "free").strip().lower() or "free",
        "guardrails": guardrail_public,
        "current_purchased_wallet_total": str(wallet_totals["purchased_total"]),
        "credits_to_grant": str(grant_amount),
        "projected_wallet_total": str(projected_wallet_total),
    }
