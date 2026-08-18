from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def normalize_subscription_provider(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"apple", "app_store", "appstore"}:
        return "apple_iap"
    if raw in {"google", "googleplay", "play"}:
        return "google_play"
    return raw


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_native_cycle_timestamp(value: Optional[datetime]) -> str:
    dt = as_utc(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def stripe_cycle_key(gateway_subscription_id: str, period_start: datetime, period_end: datetime) -> str:
    """Reconstruct the key produced from Stripe's epoch period boundaries."""
    start = as_utc(period_start)
    end = as_utc(period_end)
    if start is None or end is None:
        raise ValueError("stripe_cycle_period_required")
    sub_id = str(gateway_subscription_id or "").strip()
    if not sub_id:
        raise ValueError("stripe_subscription_id_required")
    return f"{sub_id}:{int(start.timestamp())}:{int(end.timestamp())}"


def native_cycle_key(provider: str, period_start: Optional[datetime], period_end: Optional[datetime]) -> str:
    normalized = normalize_subscription_provider(provider)
    anchor = period_end if normalized == "google_play" else period_start
    if anchor is None:
        anchor = period_start or period_end
    value = format_native_cycle_timestamp(anchor)
    if not value:
        raise ValueError("native_cycle_period_required")
    return value


def native_source_ref(provider: str, gateway_subscription_id: str, period_start: Optional[datetime], period_end: Optional[datetime]) -> str:
    normalized = normalize_subscription_provider(provider)
    sub_id = str(gateway_subscription_id or "").strip()
    if not sub_id:
        raise ValueError("native_subscription_id_required")
    return f"subscription_cycle:{normalized}:{sub_id}:{native_cycle_key(normalized, period_start, period_end)}"
