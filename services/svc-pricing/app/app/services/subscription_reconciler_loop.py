from __future__ import annotations

import asyncio
import logging
import os

from app.db import ensure_db_pool
from app.services.gateways.stripe_gateway import StripeGateway
from app.services.subscription_credit_integrity_service import repair_active_subscription_credit_cycles
from app.services.subscription_reconciler import run_subscription_reconciler_once

logger = logging.getLogger(__name__)


def reconciler_enabled() -> bool:
    raw = str(os.getenv("DF_SUBSCRIPTION_RECONCILER_ENABLED", "false")).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def credit_integrity_enabled() -> bool:
    """Run cycle-credit integrity whenever the subscription reconciler runs.

    Can be disabled independently for emergency rollback, but defaults on once
    the parent reconciler is explicitly enabled. Development V3 C2C still keeps
    the parent reconciler disabled, so no new background execution is activated
    merely by adding C6.
    """
    raw = str(os.getenv("DF_SUBSCRIPTION_CREDIT_INTEGRITY_ENABLED", "true")).strip().lower()
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
        provider_result = None
        integrity_result = None

        # Provider-state refresh and credit-cycle integrity are deliberately
        # independent failure domains. A Stripe outage must not suppress repair
        # for Apple/Google periods already persisted in our database.
        try:
            provider_result = await run_subscription_reconciler_once(
                pool,
                gw=gateway,
                lookahead_minutes=reconciler_lookahead_minutes(),
                limit=100,
            )
        except Exception:
            logger.exception("subscription_provider_reconcile_tick_failed")

        if credit_integrity_enabled():
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        integrity_result = await repair_active_subscription_credit_cycles(
                            conn,
                            limit=200,
                        )
            except Exception:
                logger.exception("subscription_credit_integrity_tick_failed")

        logger.info(
            "subscription_reconciler_tick_complete provider_count=%s credit_integrity_count=%s",
            provider_result.get("count") if isinstance(provider_result, dict) else None,
            integrity_result.get("count") if isinstance(integrity_result, dict) else None,
        )

        await asyncio.sleep(reconciler_interval_seconds())
