from __future__ import annotations

import asyncio
import os
from typing import Optional

from app.db import ensure_db_pool
from app.services.gateways.stripe_gateway import StripeGateway
from app.services.subscription_reconciler import run_subscription_reconciler_once


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
            await run_subscription_reconciler_once(
                pool,
                gw=gateway,
                lookahead_minutes=reconciler_lookahead_minutes(),
                limit=100,
            )
        except Exception:
            # Best-effort background loop: log in real implementation
            pass

        await asyncio.sleep(reconciler_interval_seconds())
