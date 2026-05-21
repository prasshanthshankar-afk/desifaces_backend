from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg


def _as_dict_loose(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


_PLAN_RANK_SQL = """
case
  when lower(coalesce(plan_code, '')) like 'enterprise%%yearly%%' then 71
  when lower(coalesce(plan_code, '')) like 'enterprise%%' then 70
  when lower(coalesce(plan_code, '')) like 'business%%yearly%%' then 61
  when lower(coalesce(plan_code, '')) like 'business%%' then 60
  when lower(coalesce(plan_code, '')) like 'pro%%yearly%%' then 51
  when lower(coalesce(plan_code, '')) like 'pro%%' then 50
  when lower(coalesce(plan_code, '')) like 'creator%%' then 40
  when lower(coalesce(plan_code, '')) = 'free' then 0
  else 10
end
"""


@dataclass(frozen=True)
class PaymentPlanSubscriptionRow:
    user_id: UUID
    gateway_provider: str
    gateway_customer_id: Optional[str]
    gateway_subscription_id: str
    gateway_price_id: Optional[str]
    plan_code: str
    subscription_state: str
    current_period_start: Optional[datetime]
    current_period_end: Optional[datetime]
    cancel_at_period_end: bool
    latest_invoice_id: Optional[str]
    latest_invoice_status: Optional[str]
    entitlement_state: str
    metadata_json: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


class PaymentPlanSubscriptionsRepo:
    """Repository for payment_plan_subscriptions only."""

    async def get_by_gateway_subscription_id(
        self,
        conn: asyncpg.Connection,
        *,
        gateway_subscription_id: str,
    ) -> Optional[PaymentPlanSubscriptionRow]:
        row = await conn.fetchrow(
            """
            select user_id, gateway_provider, gateway_customer_id, gateway_subscription_id,
                   gateway_price_id, plan_code, subscription_state, current_period_start,
                   current_period_end, cancel_at_period_end, latest_invoice_id,
                   latest_invoice_status, entitlement_state, metadata_json,
                   created_at, updated_at
            from payment_plan_subscriptions
            where gateway_subscription_id = $1
            limit 1
            """,
            gateway_subscription_id,
        )
        return self._from_row(row)

    async def get_active_by_user_id(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
    ) -> Optional[PaymentPlanSubscriptionRow]:
        row = await conn.fetchrow(
            f"""
            select user_id, gateway_provider, gateway_customer_id, gateway_subscription_id,
                   gateway_price_id, plan_code, subscription_state, current_period_start,
                   current_period_end, cancel_at_period_end, latest_invoice_id,
                   latest_invoice_status, entitlement_state, metadata_json,
                   created_at, updated_at
            from payment_plan_subscriptions
            where user_id = $1
              and entitlement_state in ('active', 'grace')
              and subscription_state in ('trialing', 'active', 'past_due', 'unpaid', 'paused')
            order by
              {_PLAN_RANK_SQL} desc,
              case when cancel_at_period_end = false then 0 else 1 end,
              current_period_end desc nulls last,
              updated_at desc,
              created_at desc
            limit 1
            """,
            user_id,
        )
        return self._from_row(row)

    async def upsert_from_gateway_subscription(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        gateway_provider: str,
        gateway_customer_id: Optional[str],
        gateway_subscription_id: str,
        gateway_price_id: Optional[str],
        plan_code: str,
        subscription_state: str,
        current_period_start: Optional[datetime],
        current_period_end: Optional[datetime],
        cancel_at_period_end: bool,
        latest_invoice_id: Optional[str],
        latest_invoice_status: Optional[str],
        entitlement_state: str,
        metadata_json: Optional[Dict[str, Any]] = None,
    ) -> PaymentPlanSubscriptionRow:
        metadata_obj = _as_dict_loose(metadata_json)

        row = await conn.fetchrow(
            """
            insert into payment_plan_subscriptions(
            user_id, gateway_provider, gateway_customer_id, gateway_subscription_id,
            gateway_price_id, plan_code, subscription_state, current_period_start,
            current_period_end, cancel_at_period_end, latest_invoice_id,
            latest_invoice_status, entitlement_state, metadata_json, created_at, updated_at
            )
            values(
            $1, $2, $3, $4,
            $5, $6, $7, $8,
            $9, $10, $11,
            $12, $13, $14::jsonb, now(), now()
            )
            on conflict (gateway_subscription_id)
            do update set
            user_id = excluded.user_id,
            gateway_provider = excluded.gateway_provider,
            gateway_customer_id = excluded.gateway_customer_id,
            gateway_price_id = excluded.gateway_price_id,
            plan_code = excluded.plan_code,
            subscription_state = excluded.subscription_state,
            current_period_start = excluded.current_period_start,
            current_period_end = excluded.current_period_end,
            cancel_at_period_end = excluded.cancel_at_period_end,
            latest_invoice_id = excluded.latest_invoice_id,
            latest_invoice_status = excluded.latest_invoice_status,
            entitlement_state = excluded.entitlement_state,
            metadata_json =
                (
                case
                    when jsonb_typeof(payment_plan_subscriptions.metadata_json) = 'object'
                    then payment_plan_subscriptions.metadata_json
                    else '{}'::jsonb
                end
                )
                ||
                (
                case
                    when jsonb_typeof(excluded.metadata_json) = 'object'
                    then jsonb_strip_nulls(excluded.metadata_json)
                    else '{}'::jsonb
                end
                ),
            updated_at = now()
            returning user_id, gateway_provider, gateway_customer_id, gateway_subscription_id,
                    gateway_price_id, plan_code, subscription_state, current_period_start,
                    current_period_end, cancel_at_period_end, latest_invoice_id,
                    latest_invoice_status, entitlement_state, metadata_json,
                    created_at, updated_at
            """,
            user_id,
            gateway_provider,
            gateway_customer_id,
            gateway_subscription_id,
            gateway_price_id,
            plan_code,
            subscription_state,
            current_period_start,
            current_period_end,
            cancel_at_period_end,
            latest_invoice_id,
            latest_invoice_status,
            entitlement_state,
            json.dumps(metadata_obj, default=str),
        )
        parsed = self._from_row(row)
        assert parsed is not None
        return parsed




    async def list_by_user_id(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
    ) -> list[PaymentPlanSubscriptionRow]:
        rows = await conn.fetch(
            f"""
            select user_id, gateway_provider, gateway_customer_id, gateway_subscription_id,
                   gateway_price_id, plan_code, subscription_state, current_period_start,
                   current_period_end, cancel_at_period_end, latest_invoice_id,
                   latest_invoice_status, entitlement_state, metadata_json,
                   created_at, updated_at
            from payment_plan_subscriptions
            where user_id = $1
            order by
              case
                when entitlement_state in ('active', 'grace')
                  and subscription_state in ('trialing', 'active', 'past_due', 'unpaid', 'paused')
                then 0 else 1
              end,
              {_PLAN_RANK_SQL} desc,
              case when cancel_at_period_end = false then 0 else 1 end,
              current_period_end desc nulls last,
              updated_at desc,
              created_at desc
            """,
            user_id,
        )
        return [self._from_row(row) for row in rows if row]

    @staticmethod
    def _from_row(row: Optional[asyncpg.Record]) -> Optional[PaymentPlanSubscriptionRow]:
        if row is None:
            return None
        return PaymentPlanSubscriptionRow(
            user_id=row["user_id"],
            gateway_provider=str(row.get("gateway_provider") or "stripe"),
            gateway_customer_id=(str(row["gateway_customer_id"]) if row.get("gateway_customer_id") is not None else None),
            gateway_subscription_id=str(row["gateway_subscription_id"]),
            gateway_price_id=(str(row["gateway_price_id"]) if row.get("gateway_price_id") is not None else None),
            plan_code=str(row.get("plan_code") or ""),
            subscription_state=str(row.get("subscription_state") or "incomplete"),
            current_period_start=row.get("current_period_start"),
            current_period_end=row.get("current_period_end"),
            cancel_at_period_end=bool(row.get("cancel_at_period_end") or False),
            latest_invoice_id=(str(row["latest_invoice_id"]) if row.get("latest_invoice_id") is not None else None),
            latest_invoice_status=(str(row["latest_invoice_status"]) if row.get("latest_invoice_status") is not None else None),
            entitlement_state=str(row.get("entitlement_state") or "inactive"),
            metadata_json=_as_dict_loose(row.get("metadata_json")),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )