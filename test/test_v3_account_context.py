from __future__ import annotations

from uuid import uuid4

import pytest

from services.shared.python.desifaces_shared.identity.account_context import (
    AccountContextNotFound,
    resolve_account_context,
)


class FakeConn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.rows.pop(0) if self.rows else None


@pytest.mark.asyncio
async def test_account_context_prefers_active_membership() -> None:
    user_id = uuid4()
    account_id = uuid4()
    conn = FakeConn(
        [
            {
                "id": account_id,
                "account_type": "business",
                "billing_mode": "hybrid",
                "default_currency": "usd",
            }
        ]
    )

    ctx = await resolve_account_context(conn, user_id)

    assert ctx.account_id == account_id
    assert ctx.user_id == user_id
    assert ctx.account_type == "business"
    assert ctx.billing_mode == "hybrid"
    assert ctx.default_currency == "USD"
    assert ctx.source == "pricing_billing_account_members"
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_account_context_falls_back_to_credit_account_link() -> None:
    user_id = uuid4()
    account_id = uuid4()
    conn = FakeConn(
        [
            None,
            {
                "id": str(account_id),
                "account_type": "individual",
                "billing_mode": "prepaid",
                "default_currency": "INR",
            },
        ]
    )

    ctx = await resolve_account_context(conn, user_id)

    assert ctx.account_id == account_id
    assert ctx.source == "pricing_credit_accounts"
    assert len(conn.calls) == 2


@pytest.mark.asyncio
async def test_account_context_falls_back_to_user_account_code() -> None:
    user_id = uuid4()
    account_id = uuid4()
    conn = FakeConn(
        [
            None,
            None,
            {
                "id": account_id,
                "account_type": "individual",
                "billing_mode": "prepaid",
                "default_currency": "USD",
            },
        ]
    )

    ctx = await resolve_account_context(conn, user_id)

    assert ctx.account_id == account_id
    assert ctx.source == "pricing_billing_accounts.user_code"
    assert conn.calls[-1][1] == (f"user:{user_id}",)


@pytest.mark.asyncio
async def test_account_context_never_synthesizes_missing_account() -> None:
    user_id = uuid4()
    conn = FakeConn([None, None, None])

    with pytest.raises(AccountContextNotFound, match=str(user_id)):
        await resolve_account_context(conn, user_id)
