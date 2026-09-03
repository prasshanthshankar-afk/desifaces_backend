from __future__ import annotations

import math
from typing import Any, Dict

PREMIUM_ACTUAL_SECONDS_VARIANT = "TALKING_VIDEO_PREMIUM_SECOND"
PREMIUM_ACTUAL_SECONDS_SKU = "LONGFORM_TALK_PREMIUM_SECOND"
PREMIUM_ACTUAL_SECONDS_ACTION = "fusion.longform.talking_video_premium_second"
PREMIUM_CREDITS_PER_SECOND = 15
PREMIUM_MIN_BILLABLE_SECONDS = 10


def _positive_seconds(value: Any) -> int:
    try:
        seconds = float(value or 0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0
    return max(1, int(math.ceil(seconds)))


def premium_billable_seconds(duration_sec: Any) -> int:
    """Canonical customer billing quantity for Premium Talking Video.

    Billing follows actual customer video duration, rounded up to the next whole
    second, with a 10-second minimum. Provider segmentation is intentionally not
    an input to this function.
    """
    actual = _positive_seconds(duration_sec)
    return max(PREMIUM_MIN_BILLABLE_SECONDS, actual or PREMIUM_MIN_BILLABLE_SECONDS)


def premium_actual_seconds_meta(duration_sec: Any) -> Dict[str, Any]:
    actual = _positive_seconds(duration_sec)
    billable = premium_billable_seconds(actual)
    return {
        "billing_basis": "actual_seconds",
        "actual_duration_sec": actual,
        "billable_seconds": billable,
        "min_billable_seconds": PREMIUM_MIN_BILLABLE_SECONDS,
        "credits_per_second": PREMIUM_CREDITS_PER_SECOND,
        "platform_neutral": True,
        "provider_neutral": True,
        "pricing_policy": "premium_actual_seconds_v1",
    }


def is_premium_actual_seconds_variant(value: Any) -> bool:
    return str(value or "").strip().upper() == PREMIUM_ACTUAL_SECONDS_VARIANT
