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


@dataclass(frozen=True)
class BillingEntitlementRow:
    user_id: UUID
    tier_code: str
    plan_code: Optional[str]
    billing_mode: str
    settlement_mode: str
    included_credits_total: int
    included_credits_remaining: int
    overage_allowed: bool
    wallet_topup_allowed: bool
    hard_stop_on_insufficient_balance: bool
    source: Optional[str]
    effective_from: datetime
    effective_to: Optional[datetime]
    updated_at: datetime
    metadata_json: Dict[str, Any] = field(default_factory=dict)


class BillingEntitlementsRepo:
    """Repository for billing_entitlements only."""

    async def get_active_by_user_id(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
    ) -> Optional[BillingEntitlementRow]:
        row = await conn.fetchrow(
            """
            select user_id, tier_code, plan_code, billing_mode, settlement_mode,
                   included_credits_total, included_credits_remaining, overage_allowed,
                   wallet_topup_allowed, hard_stop_on_insufficient_balance, source,
                   effective_from, effective_to, updated_at, metadata_json
            from billing_entitlements
            where user_id = $1
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc, updated_at desc
            limit 1
            """,
            user_id,
        )
        return self._from_row(row)

    async def upsert_billing_entitlement(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        tier_code: str,
        plan_code: Optional[str],
        billing_mode: str,
        settlement_mode: str,
        included_credits_total: int,
        included_credits_remaining: int,
        overage_allowed: bool,
        wallet_topup_allowed: bool,
        hard_stop_on_insufficient_balance: bool,
        source: str,
        metadata_json: Optional[Dict[str, Any]] = None,
    ) -> BillingEntitlementRow:
        row = await conn.fetchrow(
            """
            insert into billing_entitlements(
              user_id, tier_code, plan_code, billing_mode, settlement_mode,
              included_credits_total, included_credits_remaining, overage_allowed,
              wallet_topup_allowed, hard_stop_on_insufficient_balance, source,
              metadata_json, updated_at
            )
            values(
              $1, $2, $3, $4, $5,
              $6, $7, $8,
              $9, $10, $11,
              $12::jsonb, now()
            )
            on conflict (user_id)
            do update set
              tier_code = excluded.tier_code,
              plan_code = excluded.plan_code,
              billing_mode = excluded.billing_mode,
              settlement_mode = excluded.settlement_mode,
              included_credits_total = excluded.included_credits_total,
              included_credits_remaining = excluded.included_credits_remaining,
              overage_allowed = excluded.overage_allowed,
              wallet_topup_allowed = excluded.wallet_topup_allowed,
              hard_stop_on_insufficient_balance = excluded.hard_stop_on_insufficient_balance,
              source = excluded.source,
              metadata_json = excluded.metadata_json,
              updated_at = now()
            returning user_id, tier_code, plan_code, billing_mode, settlement_mode,
                      included_credits_total, included_credits_remaining, overage_allowed,
                      wallet_topup_allowed, hard_stop_on_insufficient_balance, source,
                      effective_from, effective_to, updated_at, metadata_json
            """,
            user_id,
            tier_code,
            plan_code,
            billing_mode,
            settlement_mode,
            int(included_credits_total),
            int(included_credits_remaining),
            overage_allowed,
            wallet_topup_allowed,
            hard_stop_on_insufficient_balance,
            source,
            json.dumps(metadata_json or {}, default=str),
        )
        parsed = self._from_row(row)
        assert parsed is not None
        return parsed

    async def set_entitlement_inactive(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        source: str,
        metadata_json: Optional[Dict[str, Any]] = None,
    ) -> Optional[BillingEntitlementRow]:
        existing = await self.get_active_by_user_id(conn, user_id=user_id)
        if existing is None:
            return None

        md = {**existing.metadata_json, **(metadata_json or {}), "deactivated_by": source}
        return await self.upsert_billing_entitlement(
            conn,
            user_id=user_id,
            tier_code="free",
            plan_code=None,
            billing_mode="free",
            settlement_mode=existing.settlement_mode,
            included_credits_total=0,
            included_credits_remaining=0,
            overage_allowed=False,
            wallet_topup_allowed=True,
            hard_stop_on_insufficient_balance=True,
            source=source,
            metadata_json=md,
        )

    async def update_cycle_grant_metadata(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        cycle_key: str,
        granted_credits: int,
        ledger_entry_id: Optional[str],
        metadata_patch: Optional[Dict[str, Any]] = None,
    ) -> Optional[BillingEntitlementRow]:
        existing = await self.get_active_by_user_id(conn, user_id=user_id)
        if existing is None:
            return None

        md = {
            **existing.metadata_json,
            **(metadata_patch or {}),
            "last_granted_cycle_key": cycle_key,
            "last_granted_credits": int(granted_credits),
            "last_grant_ledger_entry_id": ledger_entry_id,
        }
        return await self.upsert_billing_entitlement(
            conn,
            user_id=user_id,
            tier_code=existing.tier_code,
            plan_code=existing.plan_code,
            billing_mode=existing.billing_mode,
            settlement_mode=existing.settlement_mode,
            included_credits_total=existing.included_credits_total,
            included_credits_remaining=existing.included_credits_remaining,
            overage_allowed=existing.overage_allowed,
            wallet_topup_allowed=existing.wallet_topup_allowed,
            hard_stop_on_insufficient_balance=existing.hard_stop_on_insufficient_balance,
            source=existing.source or "system",
            metadata_json=md,
        )

    async def decrement_included_credits_remaining(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        amount: int,
    ) -> Optional[BillingEntitlementRow]:
        existing = await self.get_active_by_user_id(conn, user_id=user_id)
        if existing is None:
            return None
        new_value = max(0, int(existing.included_credits_remaining) - int(amount))
        return await self.set_included_credits_remaining(conn, user_id=user_id, amount=new_value)

    async def set_included_credits_remaining(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        amount: int,
    ) -> Optional[BillingEntitlementRow]:
        existing = await self.get_active_by_user_id(conn, user_id=user_id)
        if existing is None:
            return None

        return await self.upsert_billing_entitlement(
            conn,
            user_id=user_id,
            tier_code=existing.tier_code,
            plan_code=existing.plan_code,
            billing_mode=existing.billing_mode,
            settlement_mode=existing.settlement_mode,
            included_credits_total=existing.included_credits_total,
            included_credits_remaining=int(amount),
            overage_allowed=existing.overage_allowed,
            wallet_topup_allowed=existing.wallet_topup_allowed,
            hard_stop_on_insufficient_balance=existing.hard_stop_on_insufficient_balance,
            source=existing.source or "system",
            metadata_json=existing.metadata_json,
        )

    @staticmethod
    def _from_row(row: Optional[asyncpg.Record]) -> Optional[BillingEntitlementRow]:
        if row is None:
            return None
        return BillingEntitlementRow(
            user_id=row["user_id"],
            tier_code=str(row.get("tier_code") or "free"),
            plan_code=(str(row["plan_code"]) if row.get("plan_code") is not None else None),
            billing_mode=str(row.get("billing_mode") or "free"),
            settlement_mode=str(row.get("settlement_mode") or "prepaid"),
            included_credits_total=int(row.get("included_credits_total") or 0),
            included_credits_remaining=int(row.get("included_credits_remaining") or 0),
            overage_allowed=bool(row.get("overage_allowed") or False),
            wallet_topup_allowed=bool(row.get("wallet_topup_allowed") if row.get("wallet_topup_allowed") is not None else True),
            hard_stop_on_insufficient_balance=bool(row.get("hard_stop_on_insufficient_balance") if row.get("hard_stop_on_insufficient_balance") is not None else True),
            source=(str(row["source"]) if row.get("source") is not None else None),
            effective_from=row["effective_from"],
            effective_to=row.get("effective_to"),
            updated_at=row["updated_at"],
            metadata_json=_as_dict_loose(row.get("metadata_json")),
        )
