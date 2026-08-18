from __future__ import annotations

import os
from fastapi import FastAPI
from app.api import build_router


def _v3_audio_adapter_probe_enabled() -> bool:
    return str(os.getenv("DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def create_app() -> FastAPI:
    app = FastAPI(
        title=os.getenv("SERVICE_NAME", "desifaces-service"),
        version=os.getenv("SERVICE_VERSION", os.getenv("GIT_SHA", "dev")),
        docs_url=os.getenv("DOCS_URL", "/docs"),
        redoc_url=os.getenv("REDOC_URL", "/redoc"),
        openapi_url=os.getenv("OPENAPI_URL", "/openapi.json"),
    )

    app.include_router(build_router())

    # Additive V3-only read-only certification seam. Import lazily so existing
    # V2/local startup does not depend on V3 contract packaging when disabled.
    # The hidden route is excluded from OpenAPI and performs no TTS execution.
    if _v3_audio_adapter_probe_enabled():
        from app.api.v3_adapter_probe import router as v3_adapter_probe_router

        app.include_router(v3_adapter_probe_router)

    @app.get("/")
    async def root():
        return {"service": os.getenv("SERVICE_NAME", "desifaces-service"), "status": "ok"}

    return app


app = create_app()
