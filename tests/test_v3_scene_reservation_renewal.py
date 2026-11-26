"""Regression tests for V3 scene reservation lease renewal.

Run in the backend test environment with PYTHONPATH=services/svc-pricing/app.
These tests do not access an actual credit ledger.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.reservations.reservation_service import renew_reservation


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def __init__(self, row):
        self.row = row
        self.fetchrow = AsyncMock(return_value=row)
        self.fetchval = AsyncMock(
            return_value=datetime.now(timezone.utc) + timedelta(seconds=900)
        )

    def transaction(self):
        return _Tx()


@pytest.mark.asyncio
async def test_renewal_extends_same_reserved_row():
    now = datetime.now(timezone.utc)
    conn = _Conn({"status": "reserved", "expires_at": now + timedelta(seconds=60),
                  "job_ref": "stage-a"})
    with patch("app.services.reservations.reservation_service.settings",
               SimpleNamespace(MAX_RESERVATION_TTL_S=3600)):
        result = await renew_reservation(conn, user_id=__import__("uuid").uuid4(),
                                         reservation_id=__import__("uuid").uuid4(),
                                         job_ref="stage-a")
    assert result > now
    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_renewal_is_noop_when_lease_healthy():
    now = datetime.now(timezone.utc)
    conn = _Conn({"status": "reserved", "expires_at": now + timedelta(seconds=800),
                  "job_ref": "stage-a"})
    with patch("app.services.reservations.reservation_service.settings",
               SimpleNamespace(MAX_RESERVATION_TTL_S=3600)):
        result = await renew_reservation(conn, user_id=__import__("uuid").uuid4(),
                                         reservation_id=__import__("uuid").uuid4(),
                                         job_ref="stage-a")
    assert result == conn.row["expires_at"]
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("row,error", [
    ({"status": "released", "expires_at": datetime.now(timezone.utc) + timedelta(seconds=5), "job_ref": "stage-a"},
     "PRICING_INVALID_RESERVATION_STATUS"),
    ({"status": "reserved", "expires_at": datetime.now(timezone.utc) - timedelta(seconds=5), "job_ref": "stage-a"},
     "PRICING_RESERVATION_ALREADY_EXPIRED"),
    ({"status": "reserved", "expires_at": datetime.now(timezone.utc) + timedelta(seconds=5), "job_ref": "other-stage"},
     "PRICING_RESERVATION_JOB_MISMATCH"),
])
async def test_renewal_fails_closed(row, error):
    conn = _Conn(row)
    with patch("app.services.reservations.reservation_service.settings",
               SimpleNamespace(MAX_RESERVATION_TTL_S=3600)):
        with pytest.raises(ValueError, match=error):
            await renew_reservation(conn, user_id=__import__("uuid").uuid4(),
                                    reservation_id=__import__("uuid").uuid4(),
                                    job_ref="stage-a")
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_expiry_release_cannot_spend_renewed_credits():
    """The canonical release rechecks expiration under row lock, not on selection."""
    from app.services.reservations.reservation_service import release

    row = {
        "id": __import__("uuid").uuid4(),
        "status": "reserved",
        "reserved_credits": 918,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "currency": "USD",
        "estimated_money": "9.18",
        "quote_json": {},
        "allocations_json": {"allocations": [
            {"lot_id": "test-lot", "amount": 918, "bucket_type": "purchased"}
        ]},
    }
    conn = _Conn(row)
    conn.execute = AsyncMock()
    with patch("app.services.reservations.reservation_service._ensure_account_row", new_callable=AsyncMock), \
         patch("app.services.reservations.reservation_service._backfill_legacy_lots_if_needed", new_callable=AsyncMock):
        with pytest.raises(ValueError, match="PRICING_RESERVATION_RENEWED_NOT_EXPIRED"):
            await release(
                conn, user_id=__import__("uuid").uuid4(),
                reservation_id=row["id"], idempotency_key=None,
                channel="worker", country_code="", reason="expired",
            )
    conn.execute.assert_not_awaited()
