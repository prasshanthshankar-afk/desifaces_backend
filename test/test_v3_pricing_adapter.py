from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from services.shared.df_contracts.v3.commerce import (
    CreditEntryType,
    CreditReservationState,
)
from services.shared.df_contracts.v3.pricing_adapter import (
    adapt_pricing_preview_response,
    adapt_pricing_reserve_response,
    canonical_quote_id,
    credit_transaction_from_commit,
)


def _preview_payload(*, quote_id: str = "qt_legacyfingerprint123") -> dict:
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    return {
        "status": "quoted",
        "quote_id": quote_id,
        "quote_expires_at": expires.isoformat(),
        "preview_fingerprint": "f" * 64,
        "service_name": "svc-face",
        "service_action": "face.creator.generate",
        "sku_code": "FACE_CREATOR",
        "billing_mode": "bill",
        "settlement_mode": "prepaid",
        "quote_breakdown": {
            "total_credits": "12",
            "total_money": "3.45",
            "currency": "USD",
            "pricebook_id": str(uuid4()),
        },
    }


def test_legacy_quote_id_gets_stable_canonical_uuid_only_in_pricing_bridge() -> None:
    account_id = uuid4()
    user_id = uuid4()
    payload = _preview_payload()

    a = adapt_pricing_preview_response(payload, account_id=account_id, user_id=user_id)
    b = adapt_pricing_preview_response(payload, account_id=account_id, user_id=user_id)

    expected = canonical_quote_id(
        account_id=account_id,
        user_id=user_id,
        fingerprint="f" * 64,
    )
    assert a.quote.quote_id == expected
    assert b.quote.quote_id == expected
    assert a.legacy_quote_id == "qt_legacyfingerprint123"
    assert a.compatibility_metadata["legacy_quote_id"] == "qt_legacyfingerprint123"


def test_uuid_quote_id_is_preserved() -> None:
    account_id = uuid4()
    user_id = uuid4()
    quote_id = uuid4()

    result = adapt_pricing_preview_response(
        _preview_payload(quote_id=str(quote_id)),
        account_id=account_id,
        user_id=user_id,
    )

    assert result.quote.quote_id == quote_id
    assert "legacy_quote_id" not in result.compatibility_metadata


def test_preview_maps_credits_money_pricebook_and_expiry() -> None:
    account_id = uuid4()
    user_id = uuid4()
    now = datetime.now(timezone.utc)

    result = adapt_pricing_preview_response(
        _preview_payload(),
        account_id=account_id,
        user_id=user_id,
        created_at=now,
    )

    assert result.quote.account_id == account_id
    assert result.quote.user_id == user_id
    assert result.quote.operation == "face.creator.generate"
    assert result.quote.credits == 12
    assert result.quote.money is not None
    assert result.quote.money.currency == "USD"
    assert result.quote.money.amount_minor == 345
    assert result.quote.pricebook_revision
    assert result.quote.fingerprint == "f" * 64
    assert result.quote.expires_at > now


def test_preview_without_fingerprint_ignores_volatile_quote_fields() -> None:
    account_id = uuid4()
    user_id = uuid4()
    pricebook_id = str(uuid4())

    a_payload = _preview_payload(quote_id="qt_first")
    a_payload.pop("preview_fingerprint")
    a_payload["quote_expires_at"] = "2026-08-18T03:00:00+00:00"
    a_payload["quote_breakdown"]["pricebook_id"] = pricebook_id
    a_payload["quote_breakdown"].update(
        {
            "quote_id": "qt_first",
            "quote_expires_at": "2026-08-18T03:00:00+00:00",
        }
    )

    b_payload = _preview_payload(quote_id="qt_second")
    b_payload.pop("preview_fingerprint")
    b_payload["quote_expires_at"] = "2026-08-18T03:10:00+00:00"
    b_payload["quote_breakdown"]["pricebook_id"] = pricebook_id
    b_payload["quote_breakdown"].update(
        {
            "quote_id": "qt_second",
            "quote_expires_at": "2026-08-18T03:10:00+00:00",
        }
    )

    a = adapt_pricing_preview_response(a_payload, account_id=account_id, user_id=user_id)
    b = adapt_pricing_preview_response(b_payload, account_id=account_id, user_id=user_id)

    assert len(a.quote.fingerprint) == 64
    assert a.quote.fingerprint == b.quote.fingerprint
    assert a.quote.quote_id == b.quote.quote_id


def test_reserve_response_maps_to_credit_reservation() -> None:
    account_id = uuid4()
    user_id = uuid4()
    quote = adapt_pricing_preview_response(
        _preview_payload(),
        account_id=account_id,
        user_id=user_id,
    ).quote
    reservation_id = uuid4()

    result = adapt_pricing_reserve_response(
        {
            "status": "reserved",
            "reservation_id": str(reservation_id),
            "quote_id": "qt_legacyfingerprint123",
            "preview_fingerprint": "f" * 64,
            "reserved_units": "1",
        },
        quote=quote,
        account_id=account_id,
        user_id=user_id,
        reference_type="studio_job",
        reference_id=str(uuid4()),
        idempotency_key="reserve:abc",
    )

    assert result.reservation.reservation_id == reservation_id
    assert result.reservation.quote_id == quote.quote_id
    assert result.reservation.state is CreditReservationState.RESERVED
    assert result.reservation.reserved_credits == 12
    assert result.reservation.idempotency_key == "reserve:abc"


def test_reservation_requires_uuid_identity() -> None:
    account_id = uuid4()
    user_id = uuid4()
    quote = adapt_pricing_preview_response(
        _preview_payload(),
        account_id=account_id,
        user_id=user_id,
    ).quote

    try:
        adapt_pricing_reserve_response(
            {"reservation_id": "not-a-uuid"},
            quote=quote,
            account_id=account_id,
            user_id=user_id,
        )
    except ValueError as exc:
        assert "reservation_id" in str(exc)
    else:
        raise AssertionError("expected invalid reservation identity to fail")


def test_commit_maps_settlement_evidence_to_immutable_consumption_transaction() -> None:
    account_id = uuid4()
    user_id = uuid4()
    reservation_id = uuid4()
    ledger_id = uuid4()

    tx = credit_transaction_from_commit(
        account_id=account_id,
        user_id=user_id,
        reservation_id=reservation_id,
        charged_credits=7,
        balance_after=93,
        idempotency_key="commit:stable-key",
        ledger_entry_id=ledger_id,
    )

    assert tx.transaction_id == ledger_id
    assert tx.entry_type is CreditEntryType.CONSUMPTION
    assert tx.credits_delta == -7
    assert tx.balance_after == 93
    assert tx.reference_type == "credit_reservation"
    assert tx.reference_id == str(reservation_id)
    assert tx.idempotency_key == "commit:stable-key"
