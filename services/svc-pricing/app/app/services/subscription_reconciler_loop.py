from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from app.db import ensure_db_pool
from app.services.gateways.stripe_gateway import StripeGateway
from app.services.subscription_reconciler import run_subscription_reconciler_once

logger = logging.getLogger(__name__)


def reconciler_enabled() -> bool:
    raw = str(os.getenv("DF_SUBSCRIPTION_RECONCILER_ENABLED", "false")).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def reconciler_interval_seconds() -> int:
    raw = str(os.getenv("DF_SUBSCRIPTION_RECONCILER_INTERVAL_SECONDS", "900")).strip()
    try:
        value = int(raw)
        return max(60, value)
    except Exception:
        return 900


def reconciler_lookahead_minutes() -> int:
    raw = str(os.getenv("DF_SUBSCRIPTION_RECONCILER_LOOKAHEAD_MINUTES", "30")).strip()
    try:
        value = int(raw)
        return max(5, value)
    except Exception:
        return 30


async def subscription_reconciler_loop() -> None:
    if not reconciler_enabled():
        return

    pool = await ensure_db_pool()
    gateway = StripeGateway()

    while True:
        try:
            result = await run_subscription_reconciler_once(
                pool,
                gw=gateway,
                lookahead_minutes=reconciler_lookahead_minutes(),
                limit=100,
            )
            logger.info(
                "subscription_reconciler_tick_complete count=%s",
                result.get("count") if isinstance(result, dict) else None,
            )
        except Exception:
            # Best-effort background loop: never crash the API, but do not
            # swallow failures silently. Silent failure hides payment drift.
            logger.exception("subscription_reconciler_tick_failed")

        await asyncio.sleep(reconciler_interval_seconds())
