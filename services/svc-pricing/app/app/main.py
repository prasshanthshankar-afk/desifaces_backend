from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import close_db_pool, ensure_db_pool

from app.api.routes.health import router as health_router
from app.api.routes.pricing import router as pricing_router
from app.api.routes.credits import router as credits_router
from app.api.routes.reservations import router as reservations_router
from app.api.routes.payments import router as payments_router
from app.api.routes.payment_webhooks import router as payment_webhooks_router
from app.api.routes.bootstrap import router as pricing_bootstrap_router
from app.api.routes.spending import router as spending_router


logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _v3_pricing_adapter_probe_enabled() -> bool:
    return str(os.getenv("DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _attach_task_logging(task: asyncio.Task, *, task_name: str) -> None:
    def _done_callback(done: asyncio.Task) -> None:
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            logger.info("%s cancelled", task_name)
            return
        except Exception:
            logger.exception("Failed to inspect background task state: %s", task_name)
            return

        if exc is not None:
            logger.exception("%s crashed: %s", task_name, exc)

    task.add_done_callback(_done_callback)


async def _start_subscription_reconciler_best_effort(app: FastAPI) -> None:
    """
    Start the subscription reconciler as a best-effort background task.

    Important behavior:
    - Any import/config/auth/runtime failure here must NOT stop the API app.
    - The loop itself is responsible for swallowing its own periodic failures.
    """
    try:
        from app.services.subscription_reconciler_loop import (
            reconciler_enabled,
            subscription_reconciler_loop,
        )
    except Exception:
        logger.exception("Subscription reconciler import failed; continuing without reconciler")
        return

    try:
        enabled = bool(reconciler_enabled())
    except Exception:
        logger.exception("Subscription reconciler enablement check failed; continuing without reconciler")
        return

    if not enabled:
        logger.info("Subscription reconciler disabled")
        return

    try:
        task = asyncio.create_task(
            subscription_reconciler_loop(),
            name="subscription_reconciler_loop",
        )
        app.state.subscription_reconciler_task = task
        _attach_task_logging(task, task_name="subscription_reconciler_loop")
        logger.info("Subscription reconciler started")
    except Exception:
        logger.exception("Subscription reconciler failed to start; continuing without reconciler")


async def _stop_subscription_reconciler_best_effort(app: FastAPI) -> None:
    task: Optional[asyncio.Task] = getattr(app.state, "subscription_reconciler_task", None)
    if not task:
        return

    try:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("Subscription reconciler stopped")
    except Exception:
        logger.exception("Subscription reconciler shutdown failed")
    finally:
        with contextlib.suppress(Exception):
            delattr(app.state, "subscription_reconciler_task")


def create_app() -> FastAPI:
    _configure_logging()

    app = FastAPI(title=settings.SERVICE_NAME)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup() -> None:
        # DB pool is core app infrastructure; if this fails, the service should fail fast.
        await ensure_db_pool()

        # Background reconciler is optional/best-effort and must never block app startup.
        await _start_subscription_reconciler_best_effort(app)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        # Stop optional background task first, but never let failures here block shutdown.
        await _stop_subscription_reconciler_best_effort(app)

        # DB pool close should still run; shutdown should remain best-effort.
        try:
            await close_db_pool()
        except Exception:
            logger.exception("Failed to close DB pool cleanly")

    app.include_router(health_router)
    app.include_router(pricing_router)
    app.include_router(credits_router)
    app.include_router(reservations_router)
    app.include_router(payments_router)
    app.include_router(payment_webhooks_router)
    app.include_router(pricing_bootstrap_router)
    app.include_router(spending_router)

    # Additive V3-only read-only canonical mapping probe. Import lazily so V2
    # pricing startup never depends on V3 contract packaging when the flag is off.
    if _v3_pricing_adapter_probe_enabled():
        from app.api.v3_adapter_probe import router as v3_adapter_probe_router

        app.include_router(v3_adapter_probe_router)

    return app


app = create_app()
