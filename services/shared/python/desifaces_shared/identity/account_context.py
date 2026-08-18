"""Resolve canonical V3 account identity from existing desifaces billing data.

The current V2/V3 persistence already models commercial accounts in
``pricing_billing_accounts`` and membership in
``pricing_billing_account_members``.  This helper centralizes that mapping so
Face, Audio, Fusion, and Pricing do not duplicate account-resolution SQL.

It intentionally accepts a connection-like object instead of importing asyncpg,
keeping the helper lightweight and easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class AccountContext:
    account_id: UUID
    user_id: UUID
    account_type: str
    billing_mode: str
    default_currency: str
    source: str


class AccountContextNotFound(LookupError):
    pass


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


async def resolve_account_context(conn: Any, user_id: UUID) -> AccountContext:
    """Return the user's canonical default/owning billing account.

    Resolution order mirrors existing pricing behavior while preferring an
    explicit active membership:

    1. active default membership (owner/finance/member/viewer order as tie-break)
    2. active account linked from ``pricing_credit_accounts``
    3. active individual account with ``account_code = user:<uuid>``

    No account ID is synthesized.  Missing persistence is surfaced explicitly so
    callers can decide whether to block, bootstrap, or run compatibility-only.
    """

    row = await conn.fetchrow(
        """
        SELECT
            ba.id,
            ba.account_type,
            ba.billing_mode,
            ba.default_currency
        FROM public.pricing_billing_account_members bam
        JOIN public.pricing_billing_accounts ba
          ON ba.id = bam.billing_account_id
        WHERE bam.user_id = $1
          AND bam.status = 'active'
          AND ba.status = 'active'
        ORDER BY
          bam.is_default DESC,
          CASE bam.role
            WHEN 'owner' THEN 0
            WHEN 'finance_admin' THEN 1
            WHEN 'member' THEN 2
            WHEN 'viewer' THEN 3
            ELSE 4
          END,
          bam.created_at ASC
        LIMIT 1
        """,
        user_id,
    )
    if row:
        return AccountContext(
            account_id=_uuid(row["id"]),
            user_id=user_id,
            account_type=str(row.get("account_type") or "individual"),
            billing_mode=str(row.get("billing_mode") or "prepaid"),
            default_currency=str(row.get("default_currency") or "USD").upper(),
            source="pricing_billing_account_members",
        )

    row = await conn.fetchrow(
        """
        SELECT
            ba.id,
            ba.account_type,
            ba.billing_mode,
            ba.default_currency
        FROM public.pricing_credit_accounts pca
        JOIN public.pricing_billing_accounts ba
          ON ba.id = pca.billing_account_id
        WHERE pca.user_id = $1
          AND ba.status = 'active'
        LIMIT 1
        """,
        user_id,
    )
    if row:
        return AccountContext(
            account_id=_uuid(row["id"]),
            user_id=user_id,
            account_type=str(row.get("account_type") or "individual"),
            billing_mode=str(row.get("billing_mode") or "prepaid"),
            default_currency=str(row.get("default_currency") or "USD").upper(),
            source="pricing_credit_accounts",
        )

    row = await conn.fetchrow(
        """
        SELECT id, account_type, billing_mode, default_currency
        FROM public.pricing_billing_accounts
        WHERE account_code = $1
          AND status = 'active'
        LIMIT 1
        """,
        f"user:{user_id}",
    )
    if row:
        return AccountContext(
            account_id=_uuid(row["id"]),
            user_id=user_id,
            account_type=str(row.get("account_type") or "individual"),
            billing_mode=str(row.get("billing_mode") or "prepaid"),
            default_currency=str(row.get("default_currency") or "USD").upper(),
            source="pricing_billing_accounts.user_code",
        )

    raise AccountContextNotFound(f"account_context_not_found:{user_id}")
