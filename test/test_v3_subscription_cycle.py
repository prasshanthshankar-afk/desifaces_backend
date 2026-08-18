from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.shared.python.desifaces_shared.v3.subscription_cycle import (
    native_cycle_key,
    native_source_ref,
    normalize_subscription_provider,
    stripe_cycle_key,
)


def dt(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def test_stripe_cycle_key_matches_epoch_period_identity() -> None:
    start = dt(2026, 8, 1)
    end = dt(2026, 9, 1)
    assert stripe_cycle_key("sub_123", start, end) == (
        f"sub_123:{int(start.timestamp())}:{int(end.timestamp())}"
    )


def test_stripe_renewal_changes_cycle_key() -> None:
    first = stripe_cycle_key("sub_123", dt(2026, 8, 1), dt(2026, 9, 1))
    second = stripe_cycle_key("sub_123", dt(2026, 9, 1), dt(2026, 10, 1))
    assert first != second


def test_google_cycle_uses_period_end_because_purchase_token_can_stay_constant() -> None:
    assert native_cycle_key("google_play", dt(2026, 8, 1), dt(2026, 9, 1)) == "2026-09-01T00:00:00Z"
    assert native_cycle_key("google_play", dt(2026, 9, 1), dt(2026, 10, 1)) == "2026-10-01T00:00:00Z"


def test_apple_cycle_uses_current_period_start() -> None:
    assert native_cycle_key("apple_iap", dt(2026, 8, 1), dt(2026, 9, 1)) == "2026-08-01T00:00:00Z"
    assert native_source_ref("apple", "orig_123", dt(2026, 8, 1), dt(2026, 9, 1)) == (
        "subscription_cycle:apple_iap:orig_123:2026-08-01T00:00:00Z"
    )


def test_provider_aliases_are_normalized() -> None:
    assert normalize_subscription_provider("apple") == "apple_iap"
    assert normalize_subscription_provider("googleplay") == "google_play"
    assert normalize_subscription_provider("stripe") == "stripe"


def test_cycle_identity_requires_real_period_and_subscription() -> None:
    with pytest.raises(ValueError):
        native_cycle_key("apple_iap", None, None)
    with pytest.raises(ValueError):
        stripe_cycle_key("", dt(2026, 8, 1), dt(2026, 9, 1))
