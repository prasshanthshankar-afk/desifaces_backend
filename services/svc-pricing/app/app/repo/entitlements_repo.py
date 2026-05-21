from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence
from uuid import UUID

import asyncpg



def _now_utc() -> datetime:
    return datetime.now(timezone.utc)



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
class UserEntitlementRow:
    user_id: UUID
    tier_code: str
    effective_from: datetime
    billing_account_id: Optional[UUID]
    metadata_json: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeatureFlagRow:
    code: str
    scope: str
    country_code: str
    tier_code: str
    channel: str
    enabled: bool
    billing_mode: str
    effective_from: datetime
    effective_to: Optional[datetime]
    priority: int
    metadata_json: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


class EntitlementsRepo:
    """
    Canonical repository for product-plan entitlements and feature rules.

    Intended ownership:
      - pricing_user_entitlements  -> user's current plan/tier
      - pricing_feature_flags      -> feature-level rollout/access rules

    Notes:
      - This keeps product-plan state separate from billing mechanics.
      - During migration, callers may still fall back to core.users.tier.
    """

    async def get_user_entitlement(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
    ) -> Optional[UserEntitlementRow]:
        row = await conn.fetchrow(
            """
            select user_id, tier_code, effective_from, billing_account_id, metadata_json
            from pricing_user_entitlements
            where user_id = $1
            limit 1
            """,
            user_id,
        )
        return self._user_entitlement_from_row(row)

    async def ensure_default_free_entitlement(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        metadata_json: Optional[Dict[str, Any]] = None,
        billing_account_id: Optional[UUID] = None,
    ) -> UserEntitlementRow:
        existing = await self.get_user_entitlement(conn, user_id=user_id)
        if existing is not None:
            return existing

        return await self.upsert_user_entitlement(
            conn,
            user_id=user_id,
            tier_code="free",
            billing_account_id=billing_account_id,
            metadata_json={"source": "signup_default", **(metadata_json or {})},
        )

    async def upsert_user_entitlement(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        tier_code: str,
        billing_account_id: Optional[UUID] = None,
        metadata_json: Optional[Dict[str, Any]] = None,
        effective_from: Optional[datetime] = None,
    ) -> UserEntitlementRow:
        row = await conn.fetchrow(
            """
            insert into pricing_user_entitlements (
              user_id, tier_code, effective_from, metadata_json, billing_account_id
            )
            values (
              $1, $2, coalesce($3, now()), $4::jsonb, $5
            )
            on conflict (user_id)
            do update set
              tier_code = excluded.tier_code,
              effective_from = coalesce(excluded.effective_from, pricing_user_entitlements.effective_from, now()),
              metadata_json = excluded.metadata_json,
              billing_account_id = excluded.billing_account_id
            returning user_id, tier_code, effective_from, billing_account_id, metadata_json
            """,
            user_id,
            tier_code,
            effective_from,
            json.dumps(metadata_json or {}, default=str),
            billing_account_id,
        )
        parsed = self._user_entitlement_from_row(row)
        assert parsed is not None
        return parsed

    async def resolve_tier_code(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: UUID,
        fallback_to_core_user_tier: bool = True,
        ensure_default_free: bool = False,
    ) -> str:
        ent = await self.get_user_entitlement(conn, user_id=user_id)
        if ent and ent.tier_code:
            return ent.tier_code

        if fallback_to_core_user_tier:
            row = await conn.fetchrow(
                "select tier from core.users where id = $1",
                user_id,
            )
            if row and row.get("tier"):
                return str(row["tier"])

        if ensure_default_free:
            ensured = await self.ensure_default_free_entitlement(conn, user_id=user_id)
            return ensured.tier_code

        return "free"

    async def get_feature_flag(
        self,
        conn: asyncpg.Connection,
        *,
        code: str,
    ) -> Optional[FeatureFlagRow]:
        row = await conn.fetchrow(
            """
            select code, scope, country_code, tier_code, channel, enabled, billing_mode,
                   effective_from, effective_to, priority, metadata_json, created_at
            from pricing_feature_flags
            where code = $1
            limit 1
            """,
            code,
        )
        return self._feature_flag_from_row(row)

    async def list_feature_flags(
        self,
        conn: asyncpg.Connection,
        *,
        enabled_only: bool = True,
    ) -> Sequence[FeatureFlagRow]:
        if enabled_only:
            rows = await conn.fetch(
                """
                select code, scope, country_code, tier_code, channel, enabled, billing_mode,
                       effective_from, effective_to, priority, metadata_json, created_at
                from pricing_feature_flags
                where enabled = true
                order by priority desc, effective_from desc, created_at desc
                """
            )
        else:
            rows = await conn.fetch(
                """
                select code, scope, country_code, tier_code, channel, enabled, billing_mode,
                       effective_from, effective_to, priority, metadata_json, created_at
                from pricing_feature_flags
                order by priority desc, effective_from desc, created_at desc
                """
            )
        return [self._feature_flag_from_row(row) for row in rows if row]

    async def upsert_feature_flag(
        self,
        conn: asyncpg.Connection,
        *,
        code: str,
        scope: str = "global",
        country_code: str = "",
        tier_code: str = "",
        channel: str = "",
        enabled: bool = True,
        billing_mode: str = "bill",
        effective_from: Optional[datetime] = None,
        effective_to: Optional[datetime] = None,
        priority: int = 100,
        metadata_json: Optional[Dict[str, Any]] = None,
    ) -> FeatureFlagRow:
        row = await conn.fetchrow(
            """
            insert into pricing_feature_flags (
              code, scope, country_code, tier_code, channel, enabled, billing_mode,
              effective_from, effective_to, priority, metadata_json, created_at
            )
            values (
              $1, $2, $3, $4, $5, $6, $7,
              coalesce($8, now()), $9, $10, $11::jsonb, now()
            )
            on conflict (code)
            do update set
              scope = excluded.scope,
              country_code = excluded.country_code,
              tier_code = excluded.tier_code,
              channel = excluded.channel,
              enabled = excluded.enabled,
              billing_mode = excluded.billing_mode,
              effective_from = excluded.effective_from,
              effective_to = excluded.effective_to,
              priority = excluded.priority,
              metadata_json = excluded.metadata_json
            returning code, scope, country_code, tier_code, channel, enabled, billing_mode,
                      effective_from, effective_to, priority, metadata_json, created_at
            """,
            code,
            scope,
            country_code,
            tier_code,
            channel,
            enabled,
            billing_mode,
            effective_from,
            effective_to,
            priority,
            json.dumps(metadata_json or {}, default=str),
        )
        parsed = self._feature_flag_from_row(row)
        assert parsed is not None
        return parsed

    async def resolve_feature_flag(
        self,
        conn: asyncpg.Connection,
        *,
        code: str,
        tier_code: str,
        country_code: str,
        channel: str,
        now_utc: Optional[datetime] = None,
    ) -> Optional[FeatureFlagRow]:
        row = await self.get_feature_flag(conn, code=code)
        if row is None:
            return None

        when = now_utc or _now_utc()
        if row.effective_from and row.effective_from > when:
            return None
        if row.effective_to and row.effective_to <= when:
            return None
        if not row.enabled:
            return None

        md = row.metadata_json or {}

        # Explicit row-level filters are still honored if present.
        if row.country_code and row.country_code.upper() not in {country_code.upper(), "*"}:
            return None
        if row.channel and row.channel.lower() not in {channel.lower(), "*"}:
            return None
        if row.tier_code and row.tier_code.lower() not in {tier_code.lower(), "*"}:
            return None

        allowed_tiers = {str(x).strip().lower() for x in (md.get("allowed_tiers") or []) if str(x).strip()}
        denied_tiers = {str(x).strip().lower() for x in (md.get("default_denied_tiers") or []) if str(x).strip()}
        country_allow = {str(x).strip().upper() for x in (md.get("allowed_countries") or []) if str(x).strip()}
        channel_allow = {str(x).strip().lower() for x in (md.get("allowed_channels") or []) if str(x).strip()}

        if country_allow and country_code.upper() not in country_allow:
            return None
        if channel_allow and channel.lower() not in channel_allow:
            return None
        if allowed_tiers and tier_code.lower() not in allowed_tiers:
            return None
        if denied_tiers and tier_code.lower() in denied_tiers:
            return None

        return row

    @staticmethod
    def _user_entitlement_from_row(row: Optional[asyncpg.Record]) -> Optional[UserEntitlementRow]:
        if row is None:
            return None
        return UserEntitlementRow(
            user_id=row["user_id"],
            tier_code=str(row["tier_code"]),
            effective_from=row["effective_from"],
            billing_account_id=row.get("billing_account_id"),
            metadata_json=_as_dict_loose(row.get("metadata_json")),
        )

    @staticmethod
    def _feature_flag_from_row(row: Optional[asyncpg.Record]) -> Optional[FeatureFlagRow]:
        if row is None:
            return None
        return FeatureFlagRow(
            code=str(row["code"]),
            scope=str(row.get("scope") or "global"),
            country_code=str(row.get("country_code") or ""),
            tier_code=str(row.get("tier_code") or ""),
            channel=str(row.get("channel") or ""),
            enabled=bool(row.get("enabled")),
            billing_mode=str(row.get("billing_mode") or "bill"),
            effective_from=row["effective_from"],
            effective_to=row.get("effective_to"),
            priority=int(row.get("priority") or 100),
            metadata_json=_as_dict_loose(row.get("metadata_json")),
            created_at=row.get("created_at"),
        )
