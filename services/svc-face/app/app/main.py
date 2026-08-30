# services/svc-face/app/app/main.py
from __future__ import annotations

import os

from fastapi import FastAPI

from app.api import build_router
from app.db import close_pool, get_pool
from app.services.multi_person_pricing_policy import install_multi_person_pricing_policy


def _v3_face_adapter_probe_enabled() -> bool:
    return str(os.getenv("DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def create_app() -> FastAPI:
    # Pricing routing is additive and idempotent: current single-person behavior is
    # unchanged; explicit 2+ person Creator requests select FACE_MULTI_PERSON.
    install_multi_person_pricing_policy()

    app = FastAPI(
        title="desifaces Face Studio",
        version=os.getenv("SERVICE_VERSION", "1.0.0"),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Mount all API routes, including the live face_jobs router.
    app.include_router(build_router())

    # V3 adapter certification is an additive, read-only dev/runtime probe. It is
    # disabled by default and excluded from OpenAPI so the certified public Face
    # contract remains unchanged. Import lazily so existing V2/local startup does
    # not depend on V3 contract packaging when the feature is off.
    if _v3_face_adapter_probe_enabled():
        from app.api.v3_adapter_probe import router as v3_adapter_probe_router

        app.include_router(v3_adapter_probe_router)

    @app.on_event("startup")
    async def startup() -> None:
        await get_pool()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await close_pool()

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "svc-face",
            "status": "ok",
            "version": os.getenv("SERVICE_VERSION", "1.0.0"),
        }

    return app


app = create_app()