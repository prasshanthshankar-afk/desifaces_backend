from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from fastapi import FastAPI
from app.api import build_router
from app.api.routes.internal_child_pricing_runtime import install_internal_child_pricing_runtime
from app.config import settings
from app.db import get_pool
from app.services.fusion_orchestrator import FusionOrchestrator
from app.services.multi_person_pricing_policy import install_multi_person_pricing_policy

logger = logging.getLogger("svc-fusion.main")


def _v3_fusion_adapter_probe_enabled() -> bool:
    return str(os.getenv("DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _recovery_loop() -> None:
    pool = await get_pool()
    orch = FusionOrchestrator(pool)
    owner = f"fusion-recovery:{os.getpid()}"
    logger.info(
        "fusion recovery loop started batch=%s stale_seconds=%s claim_ttl_seconds=%s poll_seconds=%.2f",
        settings.FUSION_RECOVERY_BATCH_SIZE,
        settings.FUSION_RECOVERY_STALE_SECONDS,
        settings.FUSION_RECOVERY_CLAIM_TTL_SECONDS,
        float(settings.FUSION_RECOVERY_POLL_SECONDS),
    )
    while True:
        try:
            recovered = await orch.recover_stale_processing_jobs_once(
                limit=max(1, int(settings.FUSION_RECOVERY_BATCH_SIZE)),
                stale_seconds=max(15, int(settings.FUSION_RECOVERY_STALE_SECONDS)),
                claim_ttl_seconds=max(15, int(settings.FUSION_RECOVERY_CLAIM_TTL_SECONDS)),
                owner=owner,
            )
            if recovered:
                logger.info("fusion recovery processed stale jobs count=%s", recovered)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("fusion recovery loop failed")
        await asyncio.sleep(max(1.0, float(settings.FUSION_RECOVERY_POLL_SECONDS)))


def create_app() -> FastAPI:
    # Existing single-person Fusion price selection is preserved. Explicit 2+
    # participant metadata selects FUSION_MULTI_PERSON before preview/reservation.
    install_multi_person_pricing_policy()

    app = FastAPI(
        title=os.getenv("SERVICE_NAME", "desifaces-service"),
        version=os.getenv("SERVICE_VERSION", os.getenv("GIT_SHA", "dev")),
        docs_url=os.getenv("DOCS_URL", "/docs"),
        redoc_url=os.getenv("REDOC_URL", "/redoc"),
        openapi_url=os.getenv("OPENAPI_URL", "/openapi.json"),
    )

    # Canonicalize all V3 internal-child pricing responses before routes are served.
    # This keeps POST /jobs and subsequent status views aligned with the no-charge
    # parent-billing contract even when a persisted job contains a stale truthy
    # pricing snapshot from older orchestration code.
    install_internal_child_pricing_runtime()

    app.include_router(build_router())

    # Additive V3-only, read-only canonical mapping probe. Import lazily so V2
    # startup remains independent of V3 contract packaging when the flag is off.
    if _v3_fusion_adapter_probe_enabled():
        from app.api.v3_adapter_probe import router as v3_adapter_probe_router

        app.include_router(v3_adapter_probe_router)

    @app.on_event("startup")
    async def _startup() -> None:
        if bool(getattr(settings, "FUSION_RECOVERY_ENABLED", True)):
            app.state.fusion_recovery_task = asyncio.create_task(_recovery_loop())
            logger.info("fusion recovery task scheduled")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "fusion_recovery_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @app.get("/")
    async def root():
        return {"service": os.getenv("SERVICE_NAME", "desifaces-service"), "status": "ok"}

    return app


app = create_app()
